# Copyright (C) 2026 AG Projects (https://ag-projects.com)
# Licensed under the GNU General Public License, version 3 or later (see LICENSE).

"""The account's addressbook (the XCAP one SylkServer keeps, shared with Sylk Mobile, sylk-desktop
and Blink), as the gateway gives it: contacts, groups (with their contacts) and policies, cached
in ~/.sylkclient/addressbook/<account>.json.

The objects are the gateway's JSON:

    contact  id, name, uris [{id, uri, type, default}], dialog {policy, subscribe},
             presence {policy, subscribe}, attributes {...}, defaultUri
    group    id, name, contacts [contact ...], attributes {...}
    policy   id, name, uri, dialog, presence, attributes

Written contacts and groups are stamped by the server (attributes modified_by/_agent/_at/...).
"""

import json
import os
import time
import uuid

from sylkrtc import sipconfig


__all__ = ['AddressBook', 'AmbiguousError', 'new_contact', 'new_group', 'contact_uris', 'normalize_uri', 'uri_type']


class AmbiguousError(LookupError):
    def __init__(self, message, items=()):
        super().__init__(message)
        self.items = list(items)       # all the matches


def new_id():
    return str(uuid.uuid4())


def normalize_uri(text, domain):
    """user@domain (domain when missing; numbers stay as they are), without sip: or tel:, lowercase."""
    text = text.strip()
    for prefix in ('sip:', 'sips:', 'tel:'):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    if '@' not in text and not text.lstrip('+').isdigit():
        text = f'{text}@{domain}'
    return text.lower()           # SIP addresses are stored lowercase


def uri_type(text, kind=None):
    """The type an address is stored with, lowercase: the one given, else tel for a number, else sip."""
    if kind:
        return kind.lower()
    return 'tel' if text.partition('@')[0].lstrip('+').isdigit() else 'sip'


def _uri(text, kind=None):
    return dict(id=new_id(), uri=text, type=uri_type(text, kind))


def new_contact(name, uris, domain):
    """A contact dict for an add: uris are 'user', 'user@domain', or 'TYPE:uri' (e.g. mobile:+316...)."""
    entries = []
    for text in uris:
        kind = None
        head, sep, rest = text.partition('=')
        if sep and head and '@' not in head and ':' not in head:
            kind, text = head.lower(), rest   # type=uri, e.g. mobile=+31612345678; types are lowercase
        entries.append(_uri(normalize_uri(text, domain), kind))
    return dict(id=new_id(), name=name, uris=entries, dialog=dict(policy='default', subscribe=False),
                presence=dict(policy='default', subscribe=False), attributes={})


def new_group(name):
    return dict(id=new_id(), name=name, contacts=[], attributes={})


def contact_uris(contact):
    return [uri.get('uri') or '' for uri in contact.get('uris') or [] if uri.get('uri')]


def for_server(item):
    """A contact or group as sent in an update: without the picture."""
    item = dict(item)
    item.pop('avatar', None)
    if 'contacts' in item:
        item['contacts'] = [for_server(contact) if isinstance(contact, dict) else contact for contact in item['contacts'] or []]
    return item


class AddressBook(object):
    """The cached addressbook of an account."""

    def __init__(self, account_id, folder=None):
        self.account_id = account_id
        self.domain = account_id.partition('@')[2]       # an address without domain is at the account's
        self.folder = folder or os.path.join(sipconfig.DEFAULT_DIRECTORY, 'addressbook')
        self.path = os.path.join(self.folder, f'{account_id}.json')
        self.contacts = []
        self.groups = []
        self.policies = []
        self.fetched = None             # time.time() of the last fetch

    # The cache

    @property
    def cached(self):
        return self.fetched is not None

    def load(self):
        try:
            with open(self.path, encoding='utf-8') as file:
                data = json.load(file)
        except (OSError, ValueError):
            return False
        self._set(data.get('addressbook') or {})
        self.fetched = data.get('fetched')
        return True

    def save(self):
        os.makedirs(self.folder, mode=0o700, exist_ok=True)
        data = dict(account=self.account_id, fetched=self.fetched,
                    addressbook=dict(contacts=self.contacts, groups=self.groups, policies=self.policies))
        temporary = self.path + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as file:
            json.dump(data, file, indent=1, ensure_ascii=False, sort_keys=True)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def _set(self, addressbook):
        self.contacts = [contact for contact in addressbook.get('contacts') or [] if isinstance(contact, dict)]
        self.groups = [group for group in addressbook.get('groups') or [] if isinstance(group, dict)]
        self.policies = [policy for policy in addressbook.get('policies') or [] if isinstance(policy, dict)]

    def replace(self, addressbook):
        """What the server sent (account-fetch-addressbook)."""
        self._set(addressbook)
        self.fetched = time.time()
        self.save()

    def apply(self, kind, action, data):
        """A change the server confirmed (ours, or another device's)."""
        items = {'contact': self.contacts, 'group': self.groups, 'policy': self.policies}.get(kind)
        if items is None or not data.get('id'):
            return
        index = next((position for position, item in enumerate(items) if item.get('id') == data['id']), None)
        if action == 'delete':
            if index is not None:
                del items[index]
            if kind == 'contact':
                for group in self.groups:
                    group['contacts'] = [member for member in group.get('contacts') or [] if _member_id(member) != data['id']]
        elif index is None:
            items.append(data)
        else:
            items[index] = data
        if kind == 'contact' and action == 'update':
            for group in self.groups:       # a group carries copies of its contacts
                group['contacts'] = [data if _member_id(member) == data['id'] else member for member in group.get('contacts') or []]
        self.save()

    # Lookups

    def contact(self, contact_id):
        return next((contact for contact in self.contacts if contact.get('id') == contact_id), None)

    def find(self, kind, text, exact=False):
        """The contact or group named text: its id, its name, for a contact one of its addresses (user alone
        meaning user@domain of the account), else (unless exact) a unique part of its name. LookupError when
        there is none, AmbiguousError when there are several."""
        items = self.contacts if kind == 'contact' else self.groups
        text = text.strip()
        lowered = text.lower()
        address = normalize_uri(text, self.domain).lower() if kind == 'contact' and self.domain else lowered
        tests = [lambda item: item.get('id') == text,
                 lambda item: (item.get('name') or '').lower() == lowered,
                 lambda item: kind == 'contact' and any(uri.lower() in (lowered, address) for uri in contact_uris(item))]
        if not exact:
            tests.append(lambda item: lowered in (item.get('name') or '').lower())
        for test in tests:
            found = [item for item in items if test(item)]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise AmbiguousError(f'{kind} {text!r} could be ' + ', '.join(f'{item.get("name")!r} ({item.get("id")})' for item in found[:8])
                                     + ('...' if len(found) > 8 else '') + ': give its id', found)
        hint = ''
        if exact:
            close = [item.get('name') for item in items if lowered in (item.get('name') or '').lower()]
            if close:
                hint = f' (give the whole name or the id: {", ".join(repr(name) for name in close[:5])})'
        raise LookupError(f'there is no {kind} {text!r}{hint}')

    def members(self, group):
        """The contacts of a group, as the addressbook's contacts (the group's copies when not there)."""
        found = []
        for member in group.get('contacts') or []:
            contact_id = _member_id(member)
            found.append(self.contact(contact_id) or (member if isinstance(member, dict) else dict(id=contact_id, name=contact_id)))
        return found

    def groups_of(self, contact):
        return [group for group in self.groups if any(_member_id(member) == contact.get('id') for member in group.get('contacts') or [])]


def _member_id(member):
    return member.get('id') if isinstance(member, dict) else member
