#!/usr/bin/env python2
from bottle import route, run, debug, template, request, static_file, error, response, app, hook
from backend import Backend, Config, Event
from config import USERS, EVENT_LABELS, SERVERS, ENV
import os
import time
import sys
from view import page
from collections import deque
from datetime import datetime

sys.path.append('%s/beaker' % os.path.dirname(os.path.realpath(__file__)))
from beaker.middleware import SessionMiddleware

from sq_kafka import KafkaNotificationProducer

session = None


def call_func(fn_name, *args, **kwargs):
    '''
    since plugin's functions are not in the global scope, you can
    use this wrapper to call a function, whether it's in global
    scope or in one of the plugins
    '''
    if fn_name in globals():
        return globals()[fn_name](*args, **kwargs)


@hook('before_request')
def track_history():
    '''
    maintain a list of the 10 most recent pages loaded per earch particular user
    '''
    global session
    # ignore everything that's not a page being loaded by the user:
    if request.fullpath.startswith('/assets'):
        return
    # loaded in background by report page
    if request.fullpath.startswith('/report/data'):
        return
    session = request.environ.get('beaker.session')
    session['history'] = session.get('history', deque())
    # loaded in background by timeline
    if len(session['history']) and request.fullpath == '/events/xml' and session['history'][len(session['history']) - 1] == '/events/timeline':
        return
    # note the url always misses the '#foo' part
    url = request.fullpath
    if request.query_string:
        url += "?%s" % request.query_string
    if len(session['history']) and url == session['history'][len(session['history']) - 1]:
        return
    session['history'].append(url)
    if len(session['history']) > 10:
        session['history'].popleft()
    session.save()


# based on convention:
def url_to_fn_args(url):
    args = []
    if url == '/':
        fn_name = 'main'
    else:
        # filter out parameters:
        urls_with_event_id = ['/events/view/', '/events/edit/', '/events/delete/']
        for u in urls_with_event_id:
            if url.startswith(u):
                args.append(url.replace(u, ''))
                url = u[:-1]
                break
        # /foo/bar/baz -> foo_bar_baz
        fn_name = url[1:].replace('/', '_')
    return (fn_name, args)


def render_last_page(pages_to_ignore=[], **kwargs):
    last_page = '/'  # fallback
    while len(session['history']):
        candidate = session['history'].pop()
        good_candidate = True
        # never go back to anything that performs an action
        actions = ['/events/delete/']
        # ... or anything that can't display error/success mesages:
        no_msg = ['/events/csv', '/events/json', '/events/jsonp', '/events/xml']
        for page_to_ignore in actions + no_msg + pages_to_ignore:
            if page_to_ignore in candidate:
                good_candidate = False
        if good_candidate:
            last_page = candidate
            break
    fn, args = url_to_fn_args(last_page)
    print 'args', args
    print "calling last rendered page:", last_page, args, kwargs
    return call_func(fn, *args, **kwargs)


@route('/')
def main(**kwargs):
    return p(body=template('tpl/index'), page='main', **kwargs)


@route('/events/view/<event_id>')
def events_view(event_id, **kwargs):
    try:
        event = backend.get_event(event_id)
    except Exception, e:
        return render_last_page(['/events/view/'], errors=[('Could not load event', e)])
    return p(body=template('tpl/events_view', event=event), page='view', **kwargs)


@route('/events/table')
def events_table(**kwargs):
    user = request.get_cookie("user") or None
    print "User %s" % user

    events = backend.get_events_objects(limit=1000)
    # specify fields which should be used to group events and only show latest one
    # this is used to avoid cluttering up of anthracite and making it more usable
    keys_to_filter_events = {
        # "LateFiles": ['', ''],
        # "Quarantine": ['', ''],
        # "FileLoadErrors": ['', ''],
        # "ConfigWarnings": ['', ''],
        # "DataQualityCheck": ['', ''],
        "BuildFailures": ['host', 'job'],
        "etl_milestones": ['host', 'job']
    }
    # currentevents = []
    # event_group_parsed = set()

    # tag specific filtering to avoid cluttering of anthracite
    # for e in events:
    #     tag_matched = False
    #     for event_type in keys_to_filter_events:
    #         if event_type in e.tags:
    #             tag_matched = True
    #             value_list = []
    #             for key in keys_to_filter_events[event_type]:
    #                 if e.extra_attributes.get(key):
    #                     value_list.append(e.extra_attributes[key])

    #             # checking presence of keys on which we need to apply filtering
    #             # break if key is not present
    #             # ignoring such events
    #             if len(keys_to_filter_events[event_type]) != len(value_list):
    #                 break

    #             # checking if same combination has already been parsed or not
    #             # break if already parsed
    #             if (event_type, tuple(value_list)) not in event_group_parsed:
    #                 event_group_parsed.add((event_type, tuple(value_list)))
    #                 currentevents.append(e)
    #             else:
    #                 break

    #             # breaking inner loop in order to avoid appending same event twice
    #             # if it has two tags satisfying above conditions
    #             break

    #     # if tags does not match with any predefined event types then just show the event
    #     if not tag_matched:
    #         currentevents.append(e)

    return p(body=template('tpl/events_table', user=user, users=USERS, event_types=EVENT_LABELS, servers=SERVERS, events=events), page='table', **kwargs)


# similar method exists below, but we need an int timestamp
def get_event_attributes(event):

    ts = int(time.time())
    desc = event.desc
    tags = event.tags
    extra_attributes = event.extra_attributes

    return ts, desc, tags, extra_attributes


# copied logic from notifier.py
def get_recipients(recipients, priority, priority_recipients, owner):
    
    _priority_no = int(priority.replace('P', ''))
    _recipients = None

    for idx in range(_priority_no, 6):
        _current_priority = 'P' + str(idx)
        _recipients = _recipients or priority_recipients.get(_current_priority)

    _recipients = _recipients or recipients

    # adding owner of event too
    if owner not in _recipients:
        _recipients.append(owner)

    return _recipients


# clicking on the close button
@route('/events/edit/<event_id>/close', method='POST')
def events_close_post_script(event_id):
    try:
        event = backend.close_event(event_id=event_id, resolution=request.forms['resolution'])
    except Exception, e:
        return 'Could not save close event: %s. Go back to previous page to retry' % e

    # publish kafka message about closing of event so that notification can be sent
    if isinstance(event, dict):
        title = 'Closed event'
        description = 'Closed event having id {0}'.format(event['id'])
        label = 'nagbot_notifications'
        recipients = get_recipients(event['recipients'], event['current_priority'], event['priority_recipients'], event['owner'])
        producer = KafkaNotificationProducer()
        producer.produce_notification(title=title, description=description, label=label, recipients=recipients, kafka_environment=ENV)

        return render_last_page(['/events/edit/'], successes=['The event was closed'])

    else:
        response.status = 404
        response.body = 'Event not found or event is already closed'
        return response


# clicking on the reassign button
@route('/events/edit/<event_id>/reassign', method='POST')
def events_reassign_post_script(event_id):
    try:
        new_owner = request.forms['owner']
        event, old_owner = backend.reassign_event(event_id=event_id, new_owner=new_owner)
    except Exception, e:
        return 'Could not reassign event: %s. Go back to previous page to retry' % e

    if isinstance(event, dict):
        # publish kafka message about closing of event so that notification can be sent
        # sending message to old owner
        title = 'Reassigned event'
        description = 'Reassigned event having id {0} to {1}'.format(event['id'], new_owner)
        label = 'nagbot_notifications'
        recipients = [old_owner]
        producer = KafkaNotificationProducer()
        producer.produce_notification(title=title, description=description, label=label, recipients=recipients, kafka_environment=ENV)

        # sending message to new user
        title = 'Reassigned event'
        description = '{0} reassigned event having id {1} to {2}'.format(old_owner, event['id'], new_owner)
        label = 'nagbot_notifications'
        recipients = [new_owner]
        producer = KafkaNotificationProducer()
        producer.produce_notification(title=title, description=description, label=label, recipients=recipients, kafka_environment=ENV)

        return render_last_page(['/events/edit/'], successes=['The event was reassigned'])
    else:
        response.status = 404
        response.body = 'Event not found'
        return response


# clicking on the ignore button
@route('/events/edit/<event_id>/ignore', method='POST')
def events_ignore_post_script(event_id):
    try:
        event = backend.ignore_event(event_id=event_id, ignore_days=request.forms['ignore_days'], ignore_hours=request.forms['ignore_hours'])
    except Exception, e:
        return 'Could not ignore event: %s. Go back to previous page to retry' % e

    if isinstance(event, dict):
        # publish kafka message about closing of event so that notification can be sent
        title = 'Ignored event'
        description = 'Ignored event having id {0} till {1}'.format(event['id'], datetime.fromtimestamp(int(event['ignore_ends_at'])).strftime('%Y-%m-%d %H:%M:%S'))
        label = 'nagbot_notifications'
        recipients = get_recipients(event['recipients'], event['current_priority'], event['priority_recipients'], event['owner'])
        producer = KafkaNotificationProducer()
        producer.produce_notification(title=title, description=description, label=label, recipients=recipients, kafka_environment=ENV)

        return render_last_page(['/events/edit/'], successes=['The event was ignored'])

    else:
        response.status = 404
        response.body = 'Event not found or event is already closed'
        return response

@route('/events/add', method='GET')
@route('/events/add/ts=<timestamp_from_url>', method='GET')
def events_add(**kwargs):
    return p(body=template('tpl/events_add', tags=backend.get_labels(), extra_attributes=config.extra_attributes,
                           helptext=config.helptext, recommended_tags=config.recommended_tags, **kwargs), page='add', **kwargs)


@route('/session', method='POST')
def set_session():
    print "SESSION"
    user = request.forms['session']
    #response.delete_cookie("user")
    response.set_cookie("user", user)
    del request.forms['session']
    print "User %s" % request.get_cookie("user")
    return user 
    

@route('<path:re:/assets/.*>')
def static(path):
    return static_file(path, root='.')


@error(404)
def error404(code, **kwargs):
    return p(body=template('tpl/error', title='404 page not found', msg='The requested page was not found'), **kwargs)


def p(**kwargs):
    return page(config, backend, **kwargs)

app_dir = os.path.dirname(__file__)
if app_dir:
    os.chdir(app_dir)

import config
config = Config(config)
backend = Backend(config)

session_opts = {
    'session.type': 'file',
    'session.cookie_expires': 300,
    'session.data_dir': './session_data',
    'session.auto': True
}
application = app = SessionMiddleware(app(), session_opts)
if __name__ == '__main__':
    debug(True)
    #run(app=app, reloader=True, host=config.listen_host, port=config.listen_port)
    run(app=app, reloader=False, debug=True, host=config.listen_host, port=config.listen_port)
