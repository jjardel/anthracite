from types import IntType, StringType, UnicodeType
from config import EVENT_LABELS, EVENTS_TABLE, DB_HOST, DB_PORT, DB_NAME, DB_USER
import time
import datetime
import calendar
import os
import sys
import json

from sq_sql.db_client import DBClient


class Config(dict):
    '''
    based on http://stackoverflow.com/questions/4984647/accessing-dict-keys-like-an-attribute-in-python
    create a config object based on an imported module.
    with the module, you have nice config.key attrib access, but you can't config.copy() it
    so we convert it into a dict... but then we loose the nice attrib access again.
    so this class gives an object that supports both config.key and config.copy() basically
    '''
    def __init__(self, module_or_dict):
        try:
            # it's a module
            d = module_or_dict.__dict__
        except:
            # it's a dict:
            d = module_or_dict
        for k, v in d.items():
            if not k.startswith('__'):
                self[k] = v

    def __getattr__(self, attr):
        return self[attr]

    def __setattr__(self, attr, value):
        self[attr] = value

    def copy(self):
        return Config(self)


class Event():
    '''
    timestamp must be a unix timestamp (int)
    desc is a string in whatever markup you want (html usually)
    tags is a list of strings (usally simple words)
    event_id is optional, it's the elasticsearch _id field
    '''


    def __init__(self, timestamp=None, desc=None, tags=[], event_id=None, extra_attributes={}):

        assert type(timestamp) is IntType, "timestamp must be an integer: %r" % timestamp
        assert type(desc) in (StringType, UnicodeType), "desc must be a non-empty string: %r" % desc
        assert desc, "desc must be a non-empty string: %r" % desc

        self.timestamp = timestamp
        self.desc = desc
        self.tags = tags  # just a list of strings
        self.event_id = event_id
        self.extra_attributes = extra_attributes

    def __str__(self):
        pretty_desc = self.desc
        if "\n" in self.desc:
            pretty_desc = "%s..." % self.desc[:self.desc.find('\n')]
        return "Event object. event_id=%s, ts=%i, tags=%s, desc=%s" % (self.event_id, self.timestamp, ','.join(self.tags), pretty_desc)

    def __getattr__(self, nm):
        if nm == 'outage':
            for tag in self.tags:
                if tag.startswith('outage='):
                    return tag.replace('outage=', '')
            return None
        if nm == 'impact':
            for tag in self.tags:
                if tag.startswith('impact='):
                    return tag.replace('impact=', '')
            return None
        raise AttributeError("no attribute %s" % nm)


class Backend():

    def __init__(self, config=None):
        sys.path.append("%s/%s" % (os.getcwd(), 'python-dateutil'))
        sys.path.append("%s/%s" % (os.getcwd(), 'requests'))
        sys.path.append("%s/%s" % (os.getcwd(), 'rawes'))

        self.db_client = DBClient(dbName=DB_NAME, dbServer=DB_HOST,
                 dbUser=DB_USER, dbPort=DB_PORT)

    def object_to_dict(self, event):
        iso = self.unix_timestamp_to_iso8601(event.timestamp)
        data = {
            'date': iso,
            'tags': event.tags,
            'desc': event.desc
        }
        data.update(event.extra_attributes)
        return data

    def unix_timestamp_to_iso8601(self, unix_timestamp):
        return datetime.datetime.utcfromtimestamp(unix_timestamp).isoformat()

    def db_row_to_object(self, event):
        event_id = event['id']
        unix = int(event['inserted_at'])
        extra_attributes = {}
        extra_attributes['description'] = event['description']
        extra_attributes['description'] = event['description']
        extra_attributes['owner'] = event['owner']
        extra_attributes['status'] = event['status']
        extra_attributes['event_id'] = event['id']

        # adding server where event happened
        if event['extra_attributes'].get('hostname'):
            extra_attributes['host'] = event['extra_attributes'].get('hostname').replace('.private.square-root.com', '')
        else:
            extra_attributes['host'] = event['extra_attributes'].get('server').replace('.private.square-root.com', '')

        for k in event['extra_attributes']:
            if k not in ('desc', 'tags', 'date'):
                if k == 'server':
                    extra_attributes[k] = event['extra_attributes'][k].replace('.private.square-root.com', '')
                else:
                    extra_attributes[k] = event['extra_attributes'][k]

        return Event(timestamp=unix, desc=event['title'], tags=[event['label']], event_id=event_id, extra_attributes=extra_attributes)

    def close_event(self, event_id, resolution):
        query = """
            UPDATE {table_name}
            SET status='close',
                resolution='{resolution}',
                closed_at = extract(EPOCH FROM now())
            WHERE id='{event_id}' AND status='open'
            RETURNING *
            """.format(table_name=EVENTS_TABLE, resolution=resolution, event_id=event_id)

        event = self.db_client.RunQuery(query, 'list_of_dicts')
        
        if event:
            event_obj = self.db_row_to_object(event[0])
            return event_obj
        else:
            return None

    def reassign_event(self, event_id, new_owner):
        # get event info from DB and then modify
        event = self.db_client.RunQuery("""
                    SELECT *
                    FROM {table_name}
                    WHERE id='{event_id}'
                    """.format(table_name=EVENTS_TABLE, event_id=event_id), 'list_of_dicts')[0]

        old_owner = event['owner']
        recipients = event['recipients']
        priority_recipients = event['priority_recipients']

        # modifying recipients
        # remove old owner
        if old_owner in recipients:
            recipients.remove(old_owner)
            # add new owner if not present
            if new_owner not in recipients:
                recipients.append(new_owner)

        # modifying priority_recipients
        # for each level do the same as above
        for key in priority_recipients:
            recipient_list = priority_recipients[key]
            # remove old owner
            if old_owner in recipient_list:
                recipient_list.remove(old_owner)
                # add new owner if not present
                if new_owner not in recipient_list:
                    recipient_list.append(new_owner)
            # replacing in dict
            priority_recipients[key] = recipient_list

        query = """
            UPDATE {table_name}
            SET owner = '{owner}',
                recipients = '{recipients}',
                priority_recipients = '{priority_recipients}'
            WHERE id='{event_id}'
            RETURNING *
            """.format(table_name=EVENTS_TABLE, owner=new_owner, recipients=json.dumps(recipients), priority_recipients=json.dumps(priority_recipients), event_id=event_id)

        event = self.db_client.RunQuery(query, 'list_of_dicts')
        if event:
            event_obj = self.db_row_to_object(event[0])
            return event_obj
        else:
            return None

    def ignore_event(self, event_id, ignore_days, ignore_hours):
        ignore_days = ignore_days if ignore_days else 0
        ignore_hours = ignore_hours if ignore_hours else 0

        query = """
            UPDATE {table_name}
            SET status='ignore',
                ignore_starts_at=extract(EPOCH FROM now()),
                ignore_ends_at=(extract(EPOCH FROM now()) + (86400 * {ignore_days}) + (3600 * {ignore_hours}))
            WHERE id='{event_id}' AND status IN ('open', 'ignore')
            RETURNING *
            """.format(table_name=EVENTS_TABLE, ignore_days=ignore_days, ignore_hours=ignore_hours, event_id=event_id)

        event = self.db_client.RunQuery(query, 'list_of_dicts')
        if event:
            event_obj = self.db_row_to_object(event[0])
            return event_obj
        else:
            return None

    @staticmethod
    def prepare_label_match_query():
        in_clause = '\'' + '\',\''.join(EVENT_LABELS) + '\''
        return in_clause

    def db_get_events(self, query=None, limit=500):
        if query is None:
            query = """
            SELECT *
            FROM {table_name}
            WHERE label IN ({in_clause})
            LIMIT {limit}
            """.format(table_name=EVENTS_TABLE, in_clause=self.prepare_label_match_query(), limit=limit)

        return self.db_client.RunQuery(query, 'list_of_dicts')

    def get_events_objects(self, limit=500):
        # retuns a list of event objects
        events = self.db_get_events(limit=limit)
        return [self.db_row_to_object(event) for event in events]

    def get_event(self, event_id):
        # http://localhost:9200/dieterfoobarbaz/event/PZ1su5w5Stmln_c2Kc4B2g
        event = self.db_client.RunQuery("""
            SELECT *
            FROM {table_name}
            WHERE id='{event_id}'
            """.format(table_name=EVENTS_TABLE, event_id=event_id), 'list_of_dicts')
        event_obj = self.db_row_to_object(event[0])
        return event_obj

    def get_labels(self):
        # get all different labels
        labels = EVENT_LABELS
        return labels

    def get_events_count(self):
        count = 0
        count = self.db_client.RunQuery("""
            SELECT COUNT(*)
            FROM {table_name}
            """.format(table_name=EVENTS_TABLE), 'list')[0]

        return count
