# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""What the Blink clients would repair in an addressbook, without repairing anything.

Blink for macOS (Cocoa), Blink Qt and Sylk Mobile (with blink-addressbook.js, which
sylk-desktop uses verbatim) each heal the shared XCAP addressbook when they start:
group kinds, duplicate groups and contacts, conference and phone addresses, names that
only echo an address, the Tel and Conference groups, Blocked and the presence
policies, Deleted. They do it by their own code, and where the code disagrees one
client undoes or redoes what another did.

check(book, context) applies the rules of each client to the addressbook in memory and
returns the findings: for every contact or group, what each client would write. A
finding where the clients would write different things is an inconsistency, and the
rule says what would have to change for them to agree.

The rules are ported from (2026-10):
  Cocoa   blink-cocoa HistoryManager.py (stamp_group_kinds, repair_contact_addresses,
          echoed_name_replacement, sync_blocked_with_policies, file_contacts_into_kind_groups),
          util.py (pstn_e164, is_conference_uri), AddressbookChecks.py, SMSWindowManager.py
          (mergeMessagesGroupDuplicates)
  Qt      blink-qt blink/contacts.py (ContactRepair, GroupKindStamper, _heal_presence_blocks,
          file_into_kind_groups, merge_messages_duplicates), group_kinds.py, pstn_normalize.py,
          conference.py
  Mobile  sylk-mobile app.js (syncGroupsTable, _abNormalizePstnContacts, _abFileIntoKindGroup,
          _abMigrateReservedIds) and its addressbookRules.js; blink-addressbook.js
          (contactPayload.nameReplacement, contactGroups.planKindMembership);
          spec docs/addressbook/addressbook.md
What needs local state (history, the OS address book, per-device settings, the test
numbers file) is left out; the dial plan and the conference domains are parameters.
"""

import ipaddress
import re

from collections import OrderedDict


__all__ = ['Context', 'Finding', 'check', 'CLIENTS', 'RULES', 'DESCRIPTIONS']


CLIENTS = ('cocoa', 'qt', 'mobile')
CLIENT_NAMES = {'cocoa': 'Cocoa', 'qt': 'Qt', 'mobile': 'Mobile'}

# group identities: kind -> (name, reserved id)
IDENTITIES = OrderedDict([('calls', ('Calls', '_calls')), ('tel', ('Tel', '_tel')), ('blocked', ('Blocked', '_blocked')),
                          ('conference', ('Conference', '_conference')), ('favorites', ('Favorites', '_favorites'))])
MOBILE_KINDS = ('calls', 'tel', 'blocked', 'conference', 'missed', 'favorites')
MOBILE_RESERVED = {'calls': '_calls', 'tel': '_tel', 'blocked': '_blocked', 'conference': '_conference', 'missed': '_missed', 'favorites': '_favorites'}
MOBILE_RESERVED_KEYS = {'_messages': 'messages', 'favorites': 'favorites', '_favorites': 'favorites', '_calls': 'calls', '_tel': 'tel',
                        '_blocked': 'blocked', '_conference': 'conference', '_missed': 'missed'}
DELETED_ID = '_deleted'
MESSAGES_ID = '_messages'

# what each rule checks, in a line
DESCRIPTIONS = OrderedDict([
    ('group-kind', 'the Calls, Tel, Blocked, Conference, Favorites (and Missed) groups carry their kind'),
    ('group-fold', 'no two groups with the same kind, reserved id or name'),
    ('reserved-id', 'a kinded group has its reserved id (_tel, _blocked, ...)'),
    ('purge-group', 'no kind-less group named "contact" or "[...]"'),
    ('calls-group', 'there is a Calls group'),
    ('conference-domain', 'no room on a videoconference. domain'),
    ('pstn', 'phone numbers are bare +E.164'),
    ('echo-name', 'no name that only repeats an address'),
    ('local-address', 'no Bonjour or LAN address (instance id, private or link-local IP, .local): Bonjour has no XCAP (decided)'),
    ('bad-uri', 'every address is an address: no TYPE= or label: in front, no blanks'),
    ('lowercase', 'SIP addresses and address types are lowercase (decided)'),
    ('dup-uri', 'no address twice in one contact'),
    ('dup-contact', 'no address in two contacts: they are one contact, in the groups of both (decided)'),
    ('tel-group', 'every contact with a phone number is in Tel, and only those (decided)'),
    ('conference-group', 'every room is in Conference, and only rooms (decided)'),
    ('blocked', 'Blocked members and the presence-block policies are blocked'),
    ('deleted', 'a Deleted contact is in no other group and not subscribed'),
    ('presence', 'who may see our presence is subscribed to'),
    ('room-subscription', 'no presence or dialog subscription to a conference room: the conference server takes only the conference event'),
    ('messages', 'no conference room in Messages (decided)'),
    ('orphans', 'group members are contacts'),
    ('default-uri', 'the default is one of the contact\'s addresses, one at most is marked'),
])


# what each client does now, and what would have to change in it for the three to agree (None: nothing),
# per rule: title, {client: (does, change)}
RULES = OrderedDict([
    ('group-kind', ('Group kinds', {
        'cocoa': ('stamps calls, tel, blocked, conference, favorites on the group found by kind, then name, then reserved id',
                  'also stamp Missed (kind missed), and find Favorites by the legacy id "favorites" too, as Qt does'),
        'qt': ('stamps calls, tel, blocked, conference, favorites on the group found by kind, then name, then reserved id '
               '(Favorites also by the legacy id "favorites")',
               'also stamp Missed (kind missed)'),
        'mobile': ('stamps by name only (Calls, Tel, Blocked, Conference, Missed, Favorites), any group with that name',
                   'find a kind-less group by its reserved id (_tel, ...) before its name, as Blink does, and stamp only that one'),
    })),
    ('group-fold', ('Duplicate groups', {
        'cocoa': ('never folds; uses the first group in document order and leaves the others',
                  'fold groups with the same key (kind, then reserved id, then name) into the lowest id, merging their members'),
        'qt': ('folds only groups with the same kind, into the reserved id, else the one with most members',
               'fold by the same key as mobile (kind, then reserved id, then name), keeping the lowest id'),
        'mobile': ('folds groups with the same key (kind, then reserved id, then name) into the lowest id, merging the members', None),
    })),
    ('reserved-id', ('Groups not on their reserved id', {
        'cocoa': ('keeps existing ids, uses reserved ids for new groups only; its Blocked and Tel lookups do not fall back to the reserved id',
                  'nothing, as long as the group carries its kind; else also look up Blocked and Tel by reserved id'),
        'qt': ('keeps existing ids, uses reserved ids for new groups only', None),
        'mobile': ('moves a kinded group onto its reserved id (creates _kind, folds the old one into it)', None),
    })),
    ('purge-group', ('Junk-named groups', {
        'cocoa': ('keeps a kind-less group named "contact" or "[...]"', 'delete it, as mobile does (leftovers of old mobile versions)'),
        'qt': ('keeps a kind-less group named "contact" or "[...]"', 'delete it, as mobile does (leftovers of old mobile versions)'),
        'mobile': ('deletes a kind-less group named "contact" or "[...]"', None),
    })),
    ('calls-group', ('Calls group', {
        'cocoa': ('creates an empty Calls group (_calls, kind calls) when there is none', 'create it with the first call filed, as Qt and mobile do'),
        'qt': ('creates Calls with the first call filed', None),
        'mobile': ('creates Calls with the first call filed', None),
    })),
    ('conference-domain', ('Conference domain', {
        'cocoa': ('rewrites videoconference.X to conference.X', None),
        'qt': ('rewrites videoconference.X to conference.X', None),
        'mobile': ('keeps videoconference.X and treats it as an ordinary SIP contact: not a room, not filed in Conference',
                   'rewrite it to conference.X and treat it as a room (serverConferenceUri of blink-addressbook.js, spec 24.1)'),
    })),
    ('pstn', ('Phone numbers', {
        'cocoa': ('pstn_e164: a user part with + or 00 (or the dial plan\'s prefixes) and at least 8 digits becomes bare +E.164, '
                  'the domain dropped; rooms are not excluded',
                  'leave addresses on the conference domain alone, as mobile does'),
        'qt': ('pstn_e164, as Cocoa', 'leave addresses on the conference domain alone, as mobile does'),
        'mobile': ('canonicalPstn: any user part starting with + or 0, not on the conference domain; under 8 digits it is still '
                   'stripped of its domain and separators',
                   'rewrite only what becomes +E.164 with at least 8 digits, leave the rest as it is, as Blink does'),
    })),
    ('echo-name', ('Names that echo an address', {
        'cocoa': ('echoed_name_replacement: replaces a name that is the address, the same number, or the address on another domain; '
                  'keeps a name that is only the user part ("gogo" on gogo@..., "Carol" on carol@...); an empty name gets the user part', None),
        'qt': ('the same rule as Cocoa (sylk-desktop\'s nameReplacement too)', None),
        'mobile': ('isUriEcho: also the user part is an echo, so "Carol" on carol@... becomes carol@...; every echo and an empty name '
                   'get the full default address (a room its number)',
                   'port nameReplacement from blink-addressbook.js (spec 24.2 "Open"); an empty name gets the user part'),
    })),
    ('local-address', ('Bonjour and LAN addresses', {
        'cocoa': ('does not file Bonjour calls; refuses Bonjour and LAN addresses (util.is_lan_address) in call and message filing, '
                  'blocking and the contact editor, and purges the stored ones on start (purge_lan_addresses)', None),
        'qt': ('refuses Bonjour and LAN addresses in every write (is_lan_address: call and message filing, the contact editor, '
               'blocked contacts) and purges the stored ones on start (ContactRepair.purge_lan_addresses)', None),
        'mobile': ('has no Bonjour; never puts a LAN address (replicateContact, _abServerContactPayload) and purges the stored ones '
                   'on start (_abNormalizePstnContacts: the addresses, or the contact when it has no other)', None),
    })),
    ('bad-uri', ('Broken addresses', {
        'cocoa': ('repairs only phone numbers it recognises (pstn_e164) and videoconference.', 'strip a TYPE= or label: prefix, then repair'),
        'qt': ('the same as Cocoa', 'strip a TYPE= or label: prefix, then repair'),
        'mobile': ('repairs only phone numbers it recognises (canonicalPstn)', 'strip a TYPE= or label: prefix, then repair'),
    })),
    ('lowercase', ('Lowercase addresses and types', {
        'cocoa': ('stores addresses and types lowercase (contact editor, call filing, blocking) and lowercases the stored ones on '
                  'start (repair_contact_addresses); shows types through format_uri_type', None),
        'qt': ('stores addresses and types lowercase (contact editor, call filing) and lowercases the stored ones on start '
               '(repair_plan, reason lowercase); shows types capitalised', None),
        'mobile': ('writes addresses and types lowercase (canonicalPstn, uriType) and lowercases the stored ones on start '
                   '(_abNormalizePstnContacts)', None),
    })),
    ('dup-uri', ('Duplicate addresses in a contact', {
        'cocoa': ('removes exact duplicates (lowercased), keeps the first',
                  'compare the canonical forms, so one number in two spellings is one address, as mobile does'),
        'qt': ('removes exact duplicates (lowercased), keeps the first',
               'compare the canonical forms, so one number in two spellings is one address, as mobile does'),
        'mobile': ('collapses addresses with the same canonical form, keeps the first', None),
    })),
    ('dup-contact', ('Duplicate contacts', {
        'cocoa': ('folds any two contacts sharing an address into the lowest id, with all the addresses and the groups of both '
                  '(mergeMessagesGroupDuplicates, _mergeMembership)', None),
        'qt': ('folds any two contacts sharing an address into the lowest id, with all the addresses and the groups of both '
               '(merge_duplicates, membership_plan)', None),
        'mobile': ('folds any two contacts sharing an address into the lowest id (code-point order), with all the addresses and '
                   'the groups of both (_abFoldDuplicateContacts)', None),
    })),
    ('tel-group', ('Tel group', {
        'cocoa': ('adds contacts with a pstn_e164 address, except Deleted and Blocked members; never removes',
                  'also take out a contact without a phone number'),
        'qt': ('the same as Cocoa', 'also take out a contact without a phone number'),
        'mobile': ('adds every contact with an isPhoneNumber address (+ or 0, any length), Deleted and Blocked too; sylk-desktop '
                   '(planKindMembership) also takes out contacts that no longer qualify',
                   'use the same phone test as the canon (+E.164, at least 8 digits), skip Deleted and Blocked members, '
                   'take out a contact without a phone number (as sylk-desktop does)'),
    })),
    ('conference-group', ('Conference group', {
        'cocoa': ('adds rooms on the conference server or a domain starting conference. or videoconference., except Deleted and '
                  'Blocked members; never removes', 'also take out a contact that is not a room'),
        'qt': ('adds rooms on the conference server of an account (videoconference. read as conference.) or a remembered focus '
               'domain, except Deleted and Blocked members',
               'also take any conference.* domain, as Cocoa and mobile do, and take out a contact that is not a room'),
        'mobile': ('adds rooms on the conference domain or conference.*, not videoconference.*, and never a room starting with 0 '
                   '(an "anomaly"); Deleted and Blocked too',
                   'take videoconference.* (after the rewrite) and rooms starting with 0, skip Deleted and Blocked members, '
                   'take out a contact that is not a room'),
    })),
    ('blocked', ('Blocked and presence policies', {
        'cocoa': ('a member of Blocked gets presence block, no subscription, dialog block; a presence-block policy without a contact '
                  'gets a blocked contact; a contact matching a block policy gets presence block', None),
        'qt': ('the same as Cocoa, but without dialog block', 'also set dialog block'),
        'mobile': ('none of this in the addressbook', 'the same as Cocoa'),
    })),
    ('deleted', ('Deleted contacts', {
        'cocoa': ('asks about a contact in Deleted and in other groups: Restore or Keep Deleted', 'also turn off its subscription, as Qt does'),
        'qt': ('asks the same, and turns off the subscription of a Deleted contact', None),
        'mobile': ('hides Deleted contacts locally, checks nothing', 'ask the same, and turn off the subscription'),
    })),
    ('presence', ('Allowed to see presence, not subscribed', {
        'cocoa': ('asks: Subscribe, or Stop Sharing (presence block)', None),
        'qt': ('asks the same', None),
        'mobile': ('does not check', 'ask the same'),
    })),
    ('room-subscription', ('Subscriptions to conference rooms', {
        'cocoa': ('turns off presence and dialog subscribe of a contact whose addresses are all rooms (is_conference_uri) on start '
                  '(stop_room_subscriptions)', None),
        'qt': ('the same, rooms by the conference server of an account or any conference. or videoconference. domain '
               '(ContactRepair.stop_room_subscriptions)', None),
        'mobile': ('the same in the canonical payload, written back by the start pass (_abIsRoomOnly, _abNormalizePstnContacts); '
                   'rooms by _abIsConferenceUri, so not videoconference.',
                   'nothing once the conference-domain rule rewrites videoconference. to conference.'),
    })),
    ('messages', ('Rooms in Messages', {
        'cocoa': ('refuses rooms in Messages (addContactsToMessagesGroup) and takes out the ones filed before on start '
                  '(take_rooms_out_of_messages)', None),
        'qt': ('takes rooms out of Messages when it files history, rooms on a conference server of an account only',
               'take every conference contact out of Messages, any conference.* domain too'),
        'mobile': ('leaves rooms in Messages', 'take every conference contact out of Messages'),
    })),
    ('orphans', ('Group members that are not contacts', {
        client: ('nothing; the server prunes them on the next write of the group', None) for client in ('cocoa', 'qt', 'mobile')})),
    ('default-uri', ('Default address', {
        'cocoa': ('moves the default only with a removed duplicate', None),
        'qt': ('moves the default only with a removed duplicate', None),
        'mobile': ('marks the first address default when none is', None),
    })),
])


class Context(object):
    """What the rules need besides the addressbook.

    account: the account the addressbook is of (user@domain).
    conference_domains: the conference bridge domains (conference.sip2sip.info, the account's server).
    replace_leading_zero: the dial plan's "replace a leading 0 with" (0031), or None.
    idd_prefix: the dial plan's international access code (00 when None).
    """

    def __init__(self, account, conference_domains=(), replace_leading_zero=None, idd_prefix=None):
        self.account = account.lower()
        self.domain = self.account.partition('@')[2]
        domains = [domain.strip().lower() for domain in conference_domains if domain and domain.strip()] or ['conference.sip2sip.info']
        self.conference_domain = domains[0]         # mobile knows one
        self.conference_domains = set(domains)
        self.replace_leading_zero = replace_leading_zero or None
        self.idd_prefix = idd_prefix or None

    @property
    def mobile_conference_domain(self):
        return self.conference_domain


class Finding(object):
    """What each client would do about one contact or group (None: nothing), and for a rule we decided
    (WANTED) what all of them should do."""

    def __init__(self, rule, subject, actions, note=None, wanted=None):
        self.rule = rule
        self.subject = subject
        self.actions = {client: actions.get(client) for client in CLIENTS}
        self.note = note
        self.wanted = wanted

    @property
    def disagrees(self):
        if self.wanted is not None:
            return any(action != self.wanted for action in self.actions.values())
        return len({action or '' for action in self.actions.values()}) > 1

    def __repr__(self):
        return f'<Finding {self.rule} {self.subject}>'


# Addresses

SCHEME = re.compile(r'^(sip:|sips:)', re.IGNORECASE)
PSTN_SHAPE = re.compile(r'^\+?([0-9,\#\*]|(\(\s?0\s?\)|[-() \/\.]))+$')
PSTN_SEPARATORS = re.compile(r'(\(\s?0\s?\)|[-() \/\.])')


def _strip_scheme(uri):
    return SCHEME.sub('', (uri or '').strip())


def _split(uri):
    user, at, host = _strip_scheme(uri).partition('@')
    return user, host.split(';')[0].split(':')[0].lower() if at else ''


def blink_pstn_e164(uri, context):
    """Blink's pstn_e164 (Cocoa util.py, Qt pstn_normalize.py): the user part as bare +E.164, or None."""
    username = _strip_scheme(uri).partition('@')[0].strip()
    if not username or not PSTN_SHAPE.match(username):
        return None
    username = PSTN_SEPARATORS.sub('', username)
    rlz, idd = context.replace_leading_zero, context.idd_prefix
    if rlz and re.match(r'^0\d+$', username) and not username.startswith('00') and not (idd and username.startswith(idd)):
        username = rlz + username[1:]
    if username.startswith('+'):
        digits = username[1:]
    else:
        prefix = next((code for code in (idd, '00') if code and username.startswith(code) and len(username) > len(code)), None)
        if prefix is None:
            return None
        digits = username[len(prefix):]
    if not digits.isdigit() or len(digits) < 8:
        return None
    return '+' + digits


def blink_is_conference(uri, context):
    """Cocoa's is_conference_uri: the conference server, or a domain starting conference. or videoconference.
    (Qt: the conference server of an account, videoconference. read as conference.)."""
    user, host = _split(uri)
    if not host:
        return False
    return host in context.conference_domains or host.startswith(('conference.', 'videoconference.'))


def qt_is_conference(uri, context):
    user, host = _split(uri)
    if not host:
        return False
    if host.startswith('videoconference.'):
        host = host[len('video'):]
    return host in context.conference_domains


def server_conference_uri(uri):
    """videoconference.X -> conference.X (Cocoa, Qt, blink-addressbook.js); None when unchanged."""
    text = _strip_scheme(uri)
    user, at, host = text.partition('@')
    if at and host.lower().startswith('videoconference.'):
        return f'{user}@conference.{host[len("videoconference."):]}'
    return None


def mobile_is_phone(uri, context):
    text = (uri or '')
    if re.match(r'^\s*tel:', text, re.I):
        text = re.sub(r'^tel:(//)?', '', text.strip(), flags=re.I).split(';')[0]
        text = re.sub(r'%2b', '+', text, flags=re.I)
        text = re.sub(r'[\s\-.()]', '', text)
    username, domain = text, ''
    if '@' in text:
        parts = text.split('@')
        username, domain = parts[0].strip(), (parts[1] or '').strip().lower()
    if domain and domain == context.mobile_conference_domain:
        return False
    return re.fullmatch(r'(\+|0)[0-9\-()_\s]+', username) is not None


def mobile_canonical_pstn(uri, context):
    """sylk-mobile's canonicalPstn: the canon of a phone address (lowercased text for anything else)."""
    text = (uri or '').strip().lower()
    if not text or not mobile_is_phone(text, context):
        return text
    bare = re.sub(r'[\s\-()_.]', '', text.partition('@')[0])
    number = bare
    rule = context.replace_leading_zero
    if rule and re.fullmatch(r'0[0-9]+', number) and number[1] != '0':
        number = rule + number[1:]
    access = (context.idd_prefix or '00')
    if not number.startswith('+') and number.isdigit():
        for code in dict.fromkeys([access, '00']):
            if len(number) > len(code) and number.startswith(code):
                number = '+' + number[len(code):]
                break
    return number if re.fullmatch(r'\+[0-9]{8,}', number) else bare


def mobile_is_conference(uri, context):
    user, host = _split(uri)
    return bool(host) and (host == context.mobile_conference_domain or host.startswith('conference.'))


def mobile_is_anomaly(uri, context):
    user, host = _split(uri)
    return mobile_is_conference(uri, context) and user.startswith('0')


INSTANCE_ID = re.compile(r'^(urn:uuid:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def lan_host(uri):
    """What makes uri a Bonjour or LAN address: a Bonjour instance id (a uuid, urn:uuid:), its host when that is a
    private, loopback or link-local IP or a .local or bare 'local' host; else None."""
    text = _strip_scheme(uri).split(';')[0].strip()
    if INSTANCE_ID.match(text) or INSTANCE_ID.match(text.partition('@')[0]) and text.lower().endswith('@bonjour.local'):
        return 'a Bonjour instance id'
    host = text.rpartition('@')[2].lower()
    if not host:
        return None
    if host.startswith('['):
        address = host[1:].partition(']')[0]
    else:
        address = host.rsplit(':', 1)[0] if host.count(':') == 1 else host
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return host if address == 'local' or address.endswith('.local') else None
    return host if ip.is_private or ip.is_loopback or ip.is_link_local else None


# Names

def _same_phone_number(a, b):
    a = PSTN_SEPARATORS.sub('', _strip_scheme(a).partition('@')[0]).lstrip('+')
    b = PSTN_SEPARATORS.sub('', _strip_scheme(b).partition('@')[0]).lstrip('+')
    if not (a.isdigit() and b.isdigit()):
        return False
    if a == b:
        return True
    a, b = a.lstrip('0'), b.lstrip('0')
    short, long = sorted((a, b), key=len)
    return len(short) > 7 and long.endswith(short)


def blink_echo_replacement(name, uris, context):
    """Cocoa's echoed_name_replacement (Qt ports it; nameReplacement in blink-addressbook.js is the same rule):
    the new name, or None. Without the macOS Address Book."""
    name = (name or '').strip()
    addresses = [uri.strip() for uri in uris if uri and uri.strip()]
    if not name or not addresses:
        return None
    lowered = {}
    for address in addresses:
        lowered[address.lower()] = address
    lowered_name = name.lower()

    def replacement(address):
        if blink_is_conference(address, context):
            room = address.partition('@')[0]
            return room if room and room != name else None
        return address if address != name else None

    if lowered_name in lowered:
        return replacement(lowered[lowered_name])
    if re.search(r'[0-9]', lowered_name):
        for address in lowered.values():
            if blink_pstn_e164(address, Context(context.account, context.conference_domains)) and _same_phone_number(lowered_name, address):
                return replacement(address)
    if '@' not in lowered_name:
        return None
    local = lowered_name.partition('@')[0]
    if not local:
        return None
    if local in lowered:
        return replacement(lowered[local])
    for key, address in lowered.items():
        if key.partition('@')[0] == local:
            return replacement(address)
    return None


def _mobile_is_echo(name, uris, context):
    n = (name or '').strip().lower()
    if not n:
        return True
    for raw in uris:
        u = (raw or '').strip().lower()
        if not u:
            continue
        username = u.partition('@')[0]
        if n in (u, u.replace('@', '', 1), username):
            return True
        if mobile_is_phone(username, context):
            variants = {username}
            if username.startswith('+'):
                variants.add('00' + username[1:])
            elif username.startswith('00'):
                variants.add('+' + username[2:])
            if n in variants:
                return True
    return False


# The addressbook

def _uris(contact):
    return [uri.get('uri') for uri in contact.get('uris') or [] if uri.get('uri')]


def _default(contact):
    default = contact.get('defaultUri') or contact.get('default_uri')
    if isinstance(default, dict) and default.get('uri'):
        return default['uri']
    marked = [uri.get('uri') for uri in contact.get('uris') or [] if uri.get('uri') and uri.get('default')]
    uris = _uris(contact)
    return (marked or uris or [None])[0]


def _kind(group):
    return str((group.get('attributes') or {}).get('kind') or '').strip().lower()


def _member_ids(group):
    return [member.get('id') if isinstance(member, dict) else member for member in group.get('contacts') or []]


def _label(item):
    return f'{item.get("name") or "(no name)"!r} [{item.get("id")}]'


def _find_group(groups, kind, name, reserved=None, legacy=()):
    """kind -> name -> reserved id, first in document order (Cocoa _find_group, Qt find_group)."""
    for test in (lambda group: _kind(group) == kind,
                 lambda group: (group.get('name') or '').strip().lower() == name.lower(),
                 lambda group: reserved is not None and group.get('id') in (reserved,) + tuple(legacy)):
        for group in groups:
            if test(group):
                return group
    return None


def _mobile_group_key(group):
    kind = _kind(group)
    if kind in MOBILE_KINDS:
        return kind
    if group.get('id') in MOBILE_RESERVED_KEYS:
        return MOBILE_RESERVED_KEYS[group['id']]
    return (group.get('name') or '').strip().lower()


def _is_purge_group(group):
    name = (group.get('name') or '').strip().lower()
    return name == 'contact' or re.fullmatch(r'\[.*\]', name) is not None


def check(book, context):
    """The findings, in rule order: [Finding]."""
    contacts, groups, policies = book.contacts, book.groups, book.policies
    by_id = {contact.get('id'): contact for contact in contacts}
    findings = []
    add = findings.append

    deleted = next((group for group in groups if group.get('id') == DELETED_ID), None)
    deleted_ids = set(_member_ids(deleted)) if deleted else set()
    blocked = _find_group(groups, 'blocked', 'Blocked')            # Cocoa: kind, name; no reserved id
    blocked_ids = set(_member_ids(blocked)) if blocked else set()
    set_aside = deleted_ids | blocked_ids

    # group kinds
    for kind, (name, reserved) in IDENTITIES.items():
        cocoa = _find_group(groups, kind, name, reserved)
        qt = _find_group(groups, kind, name, reserved, legacy=('favorites',) if kind == 'favorites' else ())
        mobile = next((group for group in groups if not _is_purge_group(group) and not _kind(group)
                       and (group.get('name') or '').strip().lower() == name.lower()), None)
        targets = {client: group for client, group in (('cocoa', cocoa), ('qt', qt), ('mobile', mobile))
                   if group is not None and not _kind(group)}
        for group in {id(group): group for group in targets.values()}.values():
            add(Finding('group-kind', f'group {_label(group)}',
                        {client: f'stamp kind {kind}' if targets.get(client) is group else None for client in CLIENTS}))
        for client, group in (('cocoa', cocoa), ('qt', qt)):
            if group is not None and _kind(group) and _kind(group) != kind:
                add(Finding('group-kind', f'group {_label(group)}', {client: None},
                            note=f'found as {name} by {CLIENT_NAMES[client]} but carries kind {_kind(group)!r}: left alone'))
    for group in groups:
        if not _kind(group) and (group.get('name') or '').strip().lower() == 'missed':
            add(Finding('group-kind', f'group {_label(group)}', {'mobile': 'stamp kind missed'}))
        if _kind(group) and _kind(group) not in MOBILE_KINDS and _kind(group) not in ('test', 'deleted'):
            add(Finding('group-kind', f'group {_label(group)}', {}, note=f'unknown kind {_kind(group)!r}: no client knows it, mobile treats the group as shared (never deletes it)'))

    # duplicate groups
    buckets = OrderedDict()
    for group in groups:
        if not _is_purge_group(group):
            buckets.setdefault(_mobile_group_key(group), []).append(group)
    for key, bucket in buckets.items():
        if len(bucket) < 2:
            continue
        keeper = min(bucket, key=lambda group: str(group.get('id')))
        names = ', '.join(_label(group) for group in bucket)
        members = len({member for group in bucket for member in _member_ids(group)})
        mobile = f'keep {keeper.get("id")}, merge the members ({members}), delete ' + ', '.join(str(group.get('id')) for group in bucket if group is not keeper)
        qt = None
        kinds = {_kind(group) for group in bucket}
        if len(kinds) == 1 and next(iter(kinds)) in IDENTITIES:
            kind = next(iter(kinds))
            reserved = {IDENTITIES[kind][1]} | ({'favorites'} if kind == 'favorites' else set())
            qt_keeper = next((group for group in bucket if group.get('id') in reserved), None) or max(bucket, key=lambda group: len(_member_ids(group)))
            qt = f'keep {qt_keeper.get("id")}, merge the members ({members}), delete ' + ', '.join(str(group.get('id')) for group in bucket if group is not qt_keeper)
        add(Finding('group-fold', f'{len(bucket)} groups with key {key!r}: {names}',
                    {'cocoa': None, 'qt': qt, 'mobile': mobile},
                    note='Cocoa uses the first in document order and leaves the others'))

    # reserved ids (mobile)
    ids = {group.get('id') for group in groups}
    for group in groups:
        kind = _kind(group)
        reserved = MOBILE_RESERVED.get(kind)
        if reserved and group.get('id') != reserved and reserved not in ids:
            add(Finding('reserved-id', f'group {_label(group)} (kind {kind})',
                        {'mobile': f'create {reserved} with its members, then fold {group.get("id")} into it'}))

    # junk-named groups (mobile)
    for group in groups:
        if _is_purge_group(group) and not _kind(group):
            add(Finding('purge-group', f'group {_label(group)}', {'mobile': 'delete the group'}))

    # Calls group (Cocoa)
    if groups and _find_group(groups, 'calls', 'Calls', '_calls') is None:
        add(Finding('calls-group', 'no Calls group', {'cocoa': 'create _calls (Calls, kind calls), empty'}))

    # mobile runs its first-sync migration while its marker on the self contact is under 18
    self_contact = next((contact for contact in contacts if any(_strip_scheme(uri).lower() == context.account for uri in _uris(contact))), None)
    try:
        migrating = int(str(((self_contact or {}).get('attributes') or {}).get('MigratedVersion') or 0)) < 18
    except ValueError:
        migrating = True

    # contacts: addresses and names
    repaired = {}       # contact id -> [addresses as Blink leaves them]
    for contact in contacts:
        uris = contact.get('uris') or []
        blink_uris = []
        for entry in uris:
            uri = entry.get('uri')
            if not uri:
                continue
            conference = server_conference_uri(uri)
            if conference:
                add(Finding('conference-domain', f'{_label(contact)} {uri}',
                            {'cocoa': f'-> {conference}', 'qt': f'-> {conference}', 'mobile': None},
                            note='sylk-desktop (blink-addressbook.js) also rewrites it'))
                blink_uris.append(conference)
                continue
            blink = blink_pstn_e164(uri, context)
            blink = blink if blink and blink != uri else None
            mobile = None
            if mobile_is_phone(uri, context):
                canon = mobile_canonical_pstn(uri, context)
                mobile = canon if canon != uri else None
            if blink or mobile:
                add(Finding('pstn', f'{_label(contact)} {uri}',
                            {'cocoa': f'-> {blink}' if blink else None, 'qt': f'-> {blink}' if blink else None,
                             'mobile': f'-> {mobile}' if mobile else None}))
            blink_uris.append(blink or uri)
        repaired[contact.get('id')] = blink_uris

        # addresses on the LAN (decided: purged)
        local = [uri for uri in _uris(contact) if lan_host(uri)]
        if local:
            whole = len(local) == len(_uris(contact))
            groups_of = [group.get('name') or group.get('id') for group in groups if contact.get('id') in _member_ids(group)]
            mobile = None
            if migrating and any(re.fullmatch(r'\[.*\]|\d{1,3}(\.\d{1,3}){3}', lan_host(uri).rsplit(':', 1)[0] if not lan_host(uri).startswith('[') else lan_host(uri)) for uri in local):
                mobile = 'delete the contact (first-sync migration)'
            wanted = ('delete the contact' + (f' (in {", ".join(groups_of)})' if groups_of else '')) if whole else f'remove {", ".join(local)}'
            add(Finding('local-address', f'{_label(contact)} {", ".join(_uris(contact))}', {'mobile': wanted, 'qt': wanted, 'cocoa': wanted}, wanted=wanted))

        # broken addresses: a type or label written into the address, blanks, nothing before @
        for uri in _uris(contact):
            if lan_host(uri):
                continue            # local-address
            text = uri.strip()
            # sip:tel=+3120...@domain is a valid SIP URI (= and + are allowed in the user part), but the user is
            # 'tel=+3120...': no client takes it for a number, it does not dial
            prefix = re.match(r'^([A-Za-z][A-Za-z ._-]*)([=:])\s*(?=\S)', text)
            if prefix and prefix.group(2) == ':' and prefix.group(1).lower() in ('sip', 'sips', 'tel'):
                prefix = None
            fixed = text[prefix.end():] if prefix else text
            fixed = re.sub(r'^(sips?:|tel:)', '', fixed, flags=re.I)
            problems = []
            if prefix:
                problems.append(f'{prefix.group(0).strip()!r} in front')
            if fixed.partition('@')[1] and not fixed.partition('@')[0]:
                problems.append('nothing before @')
            user = fixed.partition('@')[0]
            if re.search(r'\s', user) and not blink_pstn_e164(fixed, context):
                problems.append('blanks')
            if problems and not user.strip():
                add(Finding('bad-uri', f'{_label(contact)} {uri!r}', {}, note=', '.join(problems), wanted='remove it'))
            elif problems:
                repaired_uri = blink_pstn_e164(fixed, context) or (mobile_canonical_pstn(fixed, context) if mobile_is_phone(fixed, context) else fixed)
                add(Finding('bad-uri', f'{_label(contact)} {uri!r}', {},
                            note=', '.join(problems), wanted=f'-> {repaired_uri}' + (f' (type {prefix.group(1)})' if prefix else '')))

        # lowercase SIP addresses and types (decided); a URL keeps its case
        changes = []
        for entry in uris:
            uri, kind = entry.get('uri') or '', entry.get('type') or ''
            url = kind.lower() == 'url' or re.match(r'^https?:', uri, re.I)
            if uri and not url and uri != uri.lower() and not re.search(r'[=\s]', uri):     # broken ones: bad-uri
                changes.append(f'{uri} -> {uri.lower()}')
            if kind and kind != kind.lower():
                changes.append(f'type {kind} -> {kind.lower()} ({uri})')
        if changes:
            add(Finding('lowercase', _label(contact), {client: '; '.join(changes) for client in CLIENTS}, wanted='; '.join(changes)))

        # duplicate addresses within the contact
        seen, duplicates = set(), []
        for uri in blink_uris:
            key = uri.strip().lower()
            if key in seen:
                duplicates.append(uri)
            seen.add(key)
        mobile_canon = [mobile_canonical_pstn(uri, context) if mobile_is_phone(uri, context) else uri.strip().lower() for uri in _uris(contact)]
        mobile_duplicates = [uri for position, uri in enumerate(mobile_canon) if uri in mobile_canon[:position]]
        if duplicates or mobile_duplicates:
            add(Finding('dup-uri', _label(contact),
                        {'cocoa': f'remove {", ".join(duplicates)}' if duplicates else None,
                         'qt': f'remove {", ".join(duplicates)}' if duplicates else None,
                         'mobile': f'collapse {", ".join(mobile_duplicates)}' if mobile_duplicates else None}))

        # echoed names
        name = contact.get('name') or ''
        blink_name = blink_echo_replacement(name, blink_uris, context)
        if not name.strip() and blink_uris:          # an empty name: the username of the default
            blink_name = blink_uris[0].partition('@')[0] or blink_uris[0]
        mobile_name = None
        canon_uris = [mobile_canonical_pstn(uri, context) if mobile_is_phone(uri, context) else uri.strip().lower() for uri in _uris(contact)]
        default = _default(contact)
        canon_default = (mobile_canonical_pstn(default, context) if default and mobile_is_phone(default, context) else (default or '').strip().lower())
        if canon_default and (_mobile_is_echo(name, _uris(contact) + canon_uris, context)
                              or (name and mobile_canonical_pstn(name, context) == canon_default)):
            wanted = canon_default.partition('@')[0] if mobile_is_conference(canon_default, context) else canon_default
            mobile_name = wanted if wanted != name else None
        if blink_name or mobile_name:
            add(Finding('echo-name', f'{_label(contact)} {", ".join(_uris(contact))}',
                        {'cocoa': f'rename {blink_name!r}' if blink_name else None, 'qt': f'rename {blink_name!r}' if blink_name else None,
                         'mobile': f'rename {mobile_name!r}' if mobile_name else None},
                        note='sylk-desktop (nameReplacement) does what Cocoa and Qt do' if blink_name != mobile_name else None))

        # default address
        addresses = [uri.lower() for uri in _uris(contact)]
        defaults = [uri.get('uri') for uri in uris if uri.get('default')]
        explicit = contact.get('defaultUri') or contact.get('default_uri')
        if isinstance(explicit, dict) and explicit.get('uri') and explicit['uri'].lower() not in addresses:
            add(Finding('default-uri', _label(contact), {}, note=f'defaultUri {explicit["uri"]} is not one of its addresses'))
        if len(defaults) > 1:
            add(Finding('default-uri', _label(contact), {}, note=f'{len(defaults)} addresses marked default: {", ".join(defaults)}'))
        if not addresses:
            add(Finding('default-uri', _label(contact), {}, note='no addresses'))

    # duplicate contacts (decided: an address in two contacts makes them one)
    def address_key(uri):
        uri = server_conference_uri(uri) or _strip_scheme(uri)
        number = blink_pstn_e164(uri, context)
        if number is None and mobile_is_phone(uri, context):
            canon = mobile_canonical_pstn(uri, context)
            number = canon if re.fullmatch(r'\+[0-9]{8,}', canon) else None
        return number or uri.split(';')[0].strip().lower()

    def clusters_of(members, keys):
        parent = {contact.get('id'): contact.get('id') for contact in members}

        def root(item):
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item
        owners = {}
        for contact in members:
            for key in keys(contact):
                if key in owners:
                    parent[root(contact.get('id'))] = root(owners[key])
                else:
                    owners[key] = contact.get('id')
        found = OrderedDict()
        for contact in members:
            found.setdefault(root(contact.get('id')), []).append(contact)
        return [cluster for cluster in found.values() if len(cluster) > 1]

    def lowest(cluster):
        return min(cluster, key=lambda contact: str(contact.get('id')))

    mobile_buckets = OrderedDict()
    for contact in contacts:
        canon = sorted({mobile_canonical_pstn(uri, context) if mobile_is_phone(uri, context) else uri.strip().lower()
                        for uri in _uris(contact) + ([_default(contact)] if _default(contact) else [])})
        if canon:
            mobile_buckets.setdefault('|'.join(canon), []).append(contact)
    mobile_losers = {}
    for bucket in mobile_buckets.values():
        if len(bucket) > 1:
            keeper = lowest(bucket)
            mobile_losers.update((contact.get('id'), keeper) for contact in bucket if contact is not keeper)
    messages = next((group for group in groups if group.get('id') == MESSAGES_ID), None)
    blink_losers = {}
    if messages:
        members = [by_id[contact_id] for contact_id in _member_ids(messages) if contact_id in by_id]
        blink_keys = lambda contact: {blink_pstn_e164(uri, context) or _strip_scheme(uri).split(';')[0].lower()
                                      for uri in repaired.get(contact.get('id')) or _uris(contact)}
        for cluster in clusters_of(members, blink_keys):
            keeper = lowest(cluster)
            blink_losers.update((contact.get('id'), keeper) for contact in cluster if contact is not keeper)

    def is_room(contact):
        return any(blink_is_conference(uri, context) or qt_is_conference(uri, context) or mobile_is_conference(uri, context)
                   for uri in _uris(contact))

    for cluster in clusters_of(contacts, lambda contact: {address_key(uri) for uri in _uris(contact)}):
        keeper = lowest(cluster)
        losers = [contact for contact in cluster if contact is not keeper]
        keeper_keys = {address_key(uri) for uri in _uris(keeper)}
        moved = []
        for contact in losers:
            for uri in _uris(contact):
                if address_key(uri) not in keeper_keys:
                    keeper_keys.add(address_key(uri))
                    moved.append(uri)
        ids = {contact.get('id') for contact in cluster}
        all_deleted = ids <= deleted_ids
        keeper_groups = {group.get('id') for group in groups if keeper.get('id') in _member_ids(group)}
        joined, refused = [], []
        for group in groups:
            if group.get('id') in keeper_groups or not ids & set(_member_ids(group)):
                continue
            label = group.get('name') or group.get('id')
            if group is deleted and not all_deleted:
                refused.append(f'{label} (the others are not deleted)')
            elif group is messages and is_room(keeper):
                refused.append(f'{label} (a room)')
            elif group is blocked:
                refused.append(f'{label} (ask: block {keeper.get("id")} too?)')
            elif _is_purge_group(group) and not _kind(group):
                continue
            else:
                joined.append(label)
        if keeper.get('id') in deleted_ids and not all_deleted:
            refused.append(f'{keeper.get("id")} leaves Deleted (the others are not deleted)')
        wanted = (f'fold {", ".join(str(contact.get("id")) for contact in losers)} into {keeper.get("id")}'
                  + (f'; addresses + {", ".join(moved)}' if moved else '')
                  + (f'; groups + {", ".join(joined)}' if joined else ''))
        mobile = [f'delete {contact.get("id")} (same addresses as {mobile_losers[contact.get("id")].get("id")}, its groups are lost)'
                  for contact in losers if contact.get('id') in mobile_losers]
        blink = [f'fold {contact.get("id")} into {blink_losers[contact.get("id")].get("id")}'
                 for contact in losers if contact.get('id') in blink_losers]
        if blink and len(blink) < len(losers):
            blink.append('the rest left alone (not in Messages)')
        elif blink:
            blink.append('addresses moved, groups not merged')
        if mobile and len(mobile) < len(losers):
            mobile.append('the rest left alone (not the same set of addresses)')
        # a client doing all of it, by its own rule, does what is wanted
        if losers and all(blink_losers.get(contact.get('id')) is keeper for contact in losers) and not joined:
            blink = [wanted]
        if losers and all(mobile_losers.get(contact.get('id')) is keeper for contact in losers) and not joined and not moved:
            mobile = [wanted]
        subject = '; '.join(f'{_label(contact)} {", ".join(_uris(contact))}' for contact in cluster)
        add(Finding('dup-contact', subject,
                    {'cocoa': wanted, 'qt': wanted, 'mobile': wanted},
                    note=('not merged: ' + ', '.join(refused)) if refused else None, wanted=wanted))

    # Tel and Conference
    for kind, blink_test, mobile_test in (('tel', lambda uri: blink_pstn_e164(uri, context) is not None, lambda uri: mobile_is_phone(uri, context)),
                                          ('conference', lambda uri: blink_is_conference(uri, context),
                                           lambda uri: mobile_is_conference(uri, context) and not mobile_is_anomaly(uri, context))):
        name, reserved = IDENTITIES[kind]
        group = _find_group(groups, kind, name, reserved)
        members = set(_member_ids(group)) if group else set()
        rule = 'tel-group' if kind == 'tel' else 'conference-group'
        qt_test = blink_test if kind == 'tel' else (lambda uri: qt_is_conference(uri, context))
        for contact in contacts:
            contact_id = contact.get('id')
            uris = _uris(contact)
            blink_uris = repaired.get(contact_id) or uris
            blink_wants = contact_id not in set_aside and any(blink_test(uri) for uri in blink_uris)
            qt_wants = contact_id not in set_aside and any(qt_test(uri) for uri in blink_uris)
            mobile_wants = any(mobile_test(uri) for uri in uris)
            where = group.get('name') if group else f'{name} (to be created as {reserved})'
            actions = {'cocoa': f'add to {where}' if blink_wants and contact_id not in members else None,
                       'qt': f'add to {where}' if qt_wants and contact_id not in members else None,
                       'mobile': f'add to {where}' if mobile_wants and contact_id not in members else None}
            note = None
            if kind == 'conference' and any(mobile_is_anomaly(uri, context) for uri in uris) and contact_id not in members:
                note = 'mobile: a room starting with 0 is an anomaly, never filed'
            if contact_id in members and not any(test(uri) for uri in set(uris) | set(blink_uris)
                                                 for test in (blink_test, qt_test, mobile_test)):
                # decided: a kind group holds what its kind says, a contact that is not one is taken out
                what = 'a phone number' if kind == 'tel' else 'a conference room'
                add(Finding(rule, f'{_label(contact)} {", ".join(uris)}', {},
                            note=f'in {group.get("name")} but not {what}: Cocoa, Qt and mobile only add; '
                                 f'sylk-desktop (planKindMembership) takes it out',
                            wanted=f'take it out of {group.get("name")}'))
                continue
            if any(actions.values()) or note:
                add(Finding(rule, f'{_label(contact)} {", ".join(uris)}', actions, note=note))

    # Blocked and the presence policies (Cocoa, Qt)
    if blocked is not None:
        for contact_id in _member_ids(blocked):
            contact = by_id.get(contact_id)
            if contact is None:
                continue
            presence, dialog = contact.get('presence') or {}, contact.get('dialog') or {}
            cocoa = presence.get('policy') != 'block' or bool(presence.get('subscribe')) or dialog.get('policy') != 'block'
            qt = presence.get('policy') != 'block' or bool(presence.get('subscribe'))
            if cocoa or qt:
                add(Finding('blocked', f'{_label(contact)} in Blocked',
                            {'cocoa': 'presence block, no subscription, dialog block' if cocoa else None,
                             'qt': 'presence block, no subscription' if qt else None}))
    for policy in policies:
        if (policy.get('presence') or {}).get('policy') != 'block' or not policy.get('uri'):
            continue
        key = _strip_scheme(policy['uri']).split(';')[0].lower()
        owner = next((contact for contact in contacts if any(_strip_scheme(uri).split(';')[0].lower() == key or
                                                             (blink_pstn_e164(uri, context) and blink_pstn_e164(uri, context) == blink_pstn_e164(key, context))
                                                             for uri in _uris(contact))), None)
        if owner is None:
            add(Finding('blocked', f'presence-block policy {policy["uri"]}',
                        {'cocoa': f'create a blocked contact {key} in Blocked', 'qt': f'create a blocked contact {key} in Blocked'}))
        elif owner.get('id') not in blocked_ids and (owner.get('presence') or {}).get('policy') != 'block':
            add(Finding('blocked', f'{_label(owner)} matches the presence-block policy {policy["uri"]}',
                        {'cocoa': 'presence block (not added to Blocked)', 'qt': 'presence block (not added to Blocked)'}))

    # Deleted
    if deleted is not None:
        for contact_id in _member_ids(deleted):
            contact = by_id.get(contact_id)
            if contact is None:
                continue
            others = [group.get('name') or group.get('id') for group in groups if group is not deleted and contact_id in _member_ids(group)]
            if others:
                add(Finding('deleted', f'{_label(contact)} in Deleted and in {", ".join(others)}',
                            {'cocoa': 'ask: Restore or Keep Deleted', 'qt': 'ask: Restore or Keep Deleted'}))
            if (contact.get('presence') or {}).get('subscribe'):
                add(Finding('deleted', f'{_label(contact)} in Deleted, subscribed', {'qt': 'no subscription'}))

    # presence allowed, not subscribed
    own = {context.account}
    for contact in contacts:
        presence = contact.get('presence') or {}
        if presence.get('policy') != 'allow' or presence.get('subscribe') or contact.get('id') in set_aside:
            continue
        uris = _uris(contact)
        if not uris or any(_strip_scheme(uri).split(';')[0].lower() in own for uri in uris):
            continue
        if all(blink_is_conference(uri, context) for uri in uris) or all(re.match(r'^\+?[0-9][0-9 ()./-]{2,}$', _strip_scheme(uri).partition('@')[0]) for uri in uris):
            continue
        add(Finding('presence', f'{_label(contact)} {", ".join(uris)}', {'cocoa': 'ask: Subscribe or Stop Sharing', 'qt': 'ask: Subscribe or Stop Sharing'}))

    # subscriptions to rooms: the conference server rejects every event but conference
    for contact in contacts:
        if contact.get('id') in set_aside:          # Blocked and Deleted have their own rules
            continue
        events = [event for event in ('presence', 'dialog') if (contact.get(event) or {}).get('subscribe')]
        if not events:
            continue
        rooms = [uri for uri in _uris(contact)
                 if blink_is_conference(uri, context) or qt_is_conference(uri, context) or mobile_is_conference(uri, context)]
        if not rooms:
            continue
        others = [uri for uri in _uris(contact) if uri not in rooms]
        note = f'subscribes {" and ".join(events)} to {", ".join(rooms)}'
        if others:
            # a person's address too: the subscription stays, the contact should be split
            add(Finding('room-subscription', f'{_label(contact)} {", ".join(_uris(contact))}', {},
                        note=note + f'; also has {", ".join(others)}: not only a room, the subscriptions are kept'))
            continue
        wanted = f'no {" and no ".join(events)} subscription'
        uris = _uris(contact)
        add(Finding('room-subscription', f'{_label(contact)} {", ".join(uris)}',
                    {'cocoa': wanted if all(blink_is_conference(uri, context) for uri in uris) else None,
                     'qt': wanted if all(blink_is_conference(uri, context) or qt_is_conference(uri, context) for uri in uris) else None,
                     'mobile': wanted if all(mobile_is_conference(uri, context) for uri in uris) else None},
                    note=note, wanted=wanted))

    # rooms in Messages (decided: never; Qt takes out the rooms it recognises)
    if messages is not None:
        for contact_id in _member_ids(messages):
            contact = by_id.get(contact_id)
            if contact is None or not any(blink_is_conference(uri, context) or mobile_is_conference(uri, context) for uri in _uris(contact)):
                continue
            qt = any(qt_is_conference(uri, context) for uri in _uris(contact))
            add(Finding('messages', f'{_label(contact)} {", ".join(_uris(contact))}',
                        {'qt': 'take it out of Messages' if qt else None, 'cocoa': 'take it out of Messages'}, wanted='take it out of Messages'))

    # group members that are not contacts
    for group in groups:
        missing = [str(contact_id) for contact_id in _member_ids(group) if contact_id not in by_id]
        if missing:
            add(Finding('orphans', f'group {_label(group)}', {}, note=f'{len(missing)} members that are not contacts: {", ".join(missing[:5])}'
                        + (' ...' if len(missing) > 5 else '')))
    return findings
