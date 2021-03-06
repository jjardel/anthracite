#!/usr/bin/env python2
from bottle import route, run, debug, template, request, static_file, error, response, app, hook
from backend import Backend, Event, Reportpoint, load_plugins, Config
from config import USERS, EVENT_TYPES, SERVERS
import json
import os
import time
import sys
import types
import datetime
from view import page
from collections import deque
from slacker import Slacker
import __builtin__
sys.path.append('%s/beaker' % os.path.dirname(os.path.realpath(__file__)))
from beaker.middleware import SessionMiddleware

session = None


def call_func(fn_name, *args, **kwargs):
    '''
    since plugin's functions are not in the global scope, you can
    use this wrapper to call a function, whether it's in global
    scope or in one of the plugins
    '''
    if fn_name in globals():
        return globals()[fn_name](*args, **kwargs)
    else:
        for loaded_plugin in state['loaded_plugins']:
            for attrib_key in dir(loaded_plugin):
                attrib = loaded_plugin.__dict__.get(attrib_key)
                if isinstance(attrib, types.FunctionType):
                    if attrib.__name__ == fn_name:
                        return attrib(*args, **kwargs)


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

    events = backend.get_events_objects(limit=4000)
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
    currentevents = []
    event_group_parsed = set()

    # tag specific filtering to avoid cluttering of anthracite
    for e in events:
        tag_matched = False
        for event_type in keys_to_filter_events:
            if event_type in e.tags:
                tag_matched = True
                value_list = []
                for key in keys_to_filter_events[event_type]:
                    if e.extra_attributes.get(key):
                        value_list.append(e.extra_attributes[key])

                # checking presence of keys on which we need to apply filtering
                # break if key is not present
                # ignoring such events
                if len(keys_to_filter_events[event_type]) != len(value_list):
                    break

                # checking if same combination has already been parsed or not
                # break if already parsed
                if (event_type, tuple(value_list)) not in event_group_parsed:
                    event_group_parsed.add((event_type, tuple(value_list)))
                    currentevents.append(e)
                else:
                    break

                # breaking inner loop in order to avoid appending same event twice
                # if it has two tags satisfying above conditions
                break

        # if tags does not match with any predefined event types then just show the event
        if not tag_matched:
            currentevents.append(e)

    return p(body=template('tpl/events_table', user=user, users=USERS, event_types=EVENT_TYPES, servers=SERVERS, events=currentevents), page='table', **kwargs)


@route('/events/timeline')
def events_timeline(**kwargs):
    (range_low, range_high) = backend.get_events_range()

    return p(body=template('tpl/events_timeline', range_low=range_low, range_high=range_high), page='timeline', **kwargs)


@route('/events/json')
def events_json():
    '''
    much like http://localhost:9200/anthracite/event/_search?q=*:*&pretty=true
    but: displays only the actual events, not index etc, they are sorted, and uses unix timestamps
    '''
    response.set_header("Access-Control-Allow-Origin", "*")
    response.set_header("Access-Control-Allow-Credentials", "true")
    response.set_header("Access-Control-Allow-Methods", "OPTIONS, GET, POST")
    response.set_header("Access-Control-Allow-Headers", "Content-Type, Depth, User-Agent, X-File-Size, X-Requested-With, If-Modified-Since, X-File-Name, Cache-Control")
    return {"events": backend.get_events_raw()}


@route('/events/csv')
def events_csv():
    '''
    returns the first line of every event
    '''
    response.content_type = 'text/plain'
    events = []
    for event in backend.get_events_raw():
        desc = event['desc'].replace("\n", '  ').replace("\r", ' ').strip()
        formatted = [event['id'], str(event['date']), desc, ' '.join(event['tags'])]
        events.append(','.join(formatted))
    return "\n".join(events)


@route('/events/jsonp')
def events_jsonp():
    response.content_type = 'application/x-javascript'
    jsonp = request.query.jsonp or 'jsonp'
    return '%s(%s);' % (jsonp, json.dumps(events_json()))


@route('/events/xml')
def events_xml():
    response.content_type = 'application/xml'
    return template('tpl/events_xml', events=backend.get_events_raw())


@route('/events/delete/<event_id>')
def events_delete(event_id):
    try:
        backend.delete_event(event_id)
        time.sleep(1)
    except Exception, e:
        return render_last_page([event_id], errors=[('Could not delete event', e)])
    return render_last_page([event_id], successes=['The event was deleted from the database'])


@route('/events/edit/<event_id>')
def events_edit(event_id, **kwargs):
    try:
        event = backend.get_event(event_id)
        print 'THESE ARE EXTR ATTRS'
        print event.extra_attributes['status']
    except Exception, e:
        return render_last_page(['/events/edit/'], errors=[('Could not load event', e)])
    return p(body=template('tpl/events_edit', event=event, tags=backend.get_tags()), page='edit', **kwargs)


def local_datepick_to_unix_timestamp(datepick):
    '''
    in: something like 12/31/2012 10:25:35 PM, which is local time.
    out: unix timestamp
    '''
    import time
    import datetime
    return int(time.mktime(datetime.datetime.strptime(datepick, "%m/%d/%Y %I:%M:%S %p").timetuple()))


# how does this function get called?
@route('/events/edit/<event_id>', method='POST')
def events_edit_post(event_id):
    print 'INSIDE EVENTS_EDIT_POST'
    print request.forms.keys()
    try:
        # TODO: do the same validation here as in add
        ts = local_datepick_to_unix_timestamp(request.forms.event_datetime)
        # (select2 tags form field uses comma)
        tags = request.forms.event_tags.split(',')
        desc = request.forms.event_desc

        # everything else that was sent in this request is an extra attribute

        # get rid of the 3 standard fields
        del request.forms['event_datetime']
        del request.forms['event_tags']
        del request.forms['event_desc']

        #populate extra attributes that need changing
        extra_attributes = {}
        for key in request.forms:
            extra_attributes[key] = request.forms[key]

        # if status changes from ignore to something else, put garbage in the ignore tag
        if 'status' in request.forms:
            if request.forms['status'] != 'ignore':
                extra_attributes['ignore'] = 'NA'

        event = Event(timestamp=ts, desc=desc, tags=tags, event_id=event_id, extra_attributes=extra_attributes)
    except Exception, e:
        return render_last_page(['/events/edit/'], errors=[('Could not recreate event from received information. Go back to previous page to retry', e)])
    try:
        backend.edit_event(event)
        time.sleep(1)
    except Exception, e:
        return render_last_page(['/events/edit/'], errors=[('Could not update event. Go back to previous page to retry', e)])
    return render_last_page(['/events/edit/'], successes=['The event was updated'])


# similar method exists below, but we need an int timestamp
def get_event_attributes(event):

    ts = int(time.time())
    desc = event.desc
    tags = event.tags
    extra_attributes = event.extra_attributes

    return ts, desc, tags, extra_attributes

# clicking on the comments button
@route('/events/edit/<event_id>/comment', method='POST')
def events_comment_post_script(event_id):
    try:
        print "Where does it fail?"
        event = backend.get_event(event_id)
        print "Not here"
        ts, desc, tags, extra_attributes = get_event_attributes(event)
    except Exception, e:
        response.status = 500
        return 'Could not save new event: %s. Go back to previous page to retry' % e

    if extra_attributes.has_key('comments'):
        comments = extra_attributes['comments']
    else:
        comments = ''

    new_comments_string = request.forms['comments'][:50]
    new_comments_user = request.get_cookie("user") or None

    now = datetime.datetime.now()
    new_comments_timestamp = now.strftime('%Y-%m-%d %H:%M:%S ')

    comments += '%s <b>%s:</b> %s <br>' % (new_comments_timestamp, new_comments_user, new_comments_string)
    extra_attributes['comments'] = comments

    # update the event
    event = Event(timestamp=int(ts), desc=desc, tags=tags, event_id=event_id, extra_attributes=extra_attributes)
    try:
        event_id = backend.edit_event(event)
        time.sleep(1)
        response.status = 201
        return render_last_page(['/events/edit/','/session'], successes=['The event was updated'])
    except Exception, e:
        print "Exception: %s" % e
        response.status = 500
        return 'Could not save new event: %s. Go back to previous page to retry' % e


# clicking on the close button
@route('/events/edit/<event_id>/close', method='POST')
def events_close_post_script(event_id):
    try:
        event = backend.get_event(event_id)
        ts, desc, tags, extra_attributes = get_event_attributes(event)
    except Exception, e:
        response.status = 500
        return 'Could not save new event: %s. Go back to previous page to retry' % e

    now = datetime.datetime.now()
    timestamp = now.strftime('%Y-%m-%d %H:%M:%S ')

    resolution = '%s: %s' % (timestamp, request.forms['resolution'])
    extra_attributes['resolution'] = resolution
    extra_attributes['status'] = 'closed'

    # update the event
    event = Event(timestamp=int(ts), desc=desc, tags=tags, event_id=event_id, extra_attributes=extra_attributes)

    try:
        event_id = backend.edit_event(event)
        time.sleep(1)
        response.status = 201
        if tags == ['BuildFailures']:
            job = event.extra_attributes['job']
            host = event.extra_attributes['host']
            owner = event.extra_attributes['owner']
            resolution = event.extra_attributes['resolution']

            message_dict = {
                'job': job,
                'host': host,
                'owner': owner,
                'resolution': resolution
            }
            message = '%s failure on %s closed by user %s <br> resolution: %s' % (job, host, owner, resolution)

            notify_on_close(message_dict)

        return render_last_page(['/events/edit/'], successes=['The event was updated'])
    except Exception, e:
        response.status = 500
        return 'Could not save new event: %s. Go back to previous page to retry' % e


# experimental
@route('/events/edit/<event_id>/script', method='POST')
def events_edit_post_script(event_id):

    try:
        event = backend.get_event(event_id)
        ts, desc, tags, extra_attributes = get_event_attributes(event)

        # get rid of the base attributes that get sent in an edit request
        del request.forms['event_timestamp']
        del request.forms['event_desc']


        if 'event_tags' in request.forms:
            del request.forms['event_tags']
        print '6'
        # this one comes from client-side requests
        if 'event_id' in request.forms:
            del request.forms['event_id']
        print '7'
        # populate list of attributes to update from the remaining keys in the request
        updated_attributes = {}

        for key in request.forms.keys():
            val = request.forms.getall(key)
            if val:
                if len(val) == 1:
                    val = val[0]
            updated_attributes[key] = val            
        print updated_attributes
        extra_attributes.update(updated_attributes)
        print extra_attributes
        event = Event(timestamp=int(ts), desc=desc, tags=tags, event_id=event_id, extra_attributes=extra_attributes)
        print 'IT WORKED'

    # these exceptions don't print anything to the screen.  I should fix that...
    except Exception, e:
        return 'Could not edit event: %s. Go back to previous page to retry' % e

    try:
        event_id = backend.edit_event(event)
        time.sleep(1)
        response.status = 201
        print 'ok'
        #return 'ok event_id=%s\n' % event_id
        return render_last_page(['/events/edit/'], successes=['The event was updated'])
    except Exception, e:
        response.status = 500
        return 'Could not save new event: %s. Go back to previous page to retry' % e


@route('/events/add', method='GET')
@route('/events/add/ts=<timestamp_from_url>', method='GET')
def events_add(**kwargs):
    return p(body=template('tpl/events_add', tags=backend.get_tags(), extra_attributes=config.extra_attributes,
                           helptext=config.helptext, recommended_tags=config.recommended_tags, **kwargs), page='add', **kwargs)


def add_post_validate_and_parse_base_attributes(request):
    # local_datepick_to_unix_timestamp will raise exceptions if input is bad
    ts = local_datepick_to_unix_timestamp(request.forms.event_datetime)
    desc = request.forms.event_desc
    if not desc:
        raise Exception("description must not be empty")
    tags = request.forms.getall('event_tags_recommended')
    # (select2 tags form field uses comma)
    tags.extend(request.forms.event_tags.split(','))
    return (ts, desc, tags)


def add_post_validate_and_parse_extra_attributes(request, config):
    for key in request.forms:
        print key

    extra_attributes = {}
    for attribute in config.extra_attributes:
        if attribute.mandatory:
            if attribute.key not in request.forms:
                raise Exception(attribute.key + " not found in submitted data")
            elif not request.forms[attribute.key]:
                raise Exception(attribute.key + " is empty.  you have to do better")
        # if you want to get pedantic, you can check if the received values match predefined options
        if attribute.key in request.forms and request.forms[attribute.key]:
            # for select boxes, we'll receive a POST key/value for each (so
            # same key for each value), which bottle turns into list of vals
            val = request.forms.getall(attribute.key)
            if len(val) == 1:
                val = val[0]
            extra_attributes[attribute.key] = val
    return extra_attributes


def add_post_validate_and_parse_unknown_attributes(request, config):
    # there may be fields we didn't predict (i.e. from scripts that submit
    # events programmatically).  let's just store those as additional
    # attributes.  so let's remove all attributes we already handled.
    # some attribs are optional, but we can ignore KeyErrors
    # because validation already happened
    # note also that if no checkbox is selected, that key doesn't exist
    standard_attribs = ['event_desc', 'event_datetime', 'event_timestamp', 'event_tags']
    extra_attribs = [attribute.key for attribute in config.extra_attributes]
    unknown_attributes = {}
    for attrib in (standard_attribs + extra_attribs):
        try:
            del request.forms[attrib]
        except KeyError:
            pass
    # only the extra fields remain. get rid of entries with
    # empty values, and store them.
    # (this *should* work for strings and lists...)
    for key in request.forms.keys():
        val = request.forms.getall(key)
        if val:
            if len(val) == 1:
                val = val[0]
            unknown_attributes[key] = val
    return unknown_attributes


def add_post_handler_default(request, config):
    (ts, desc, tags) = add_post_validate_and_parse_base_attributes(request)
    extra_attributes = add_post_validate_and_parse_extra_attributes(request, config)
    unknown_attributes = add_post_validate_and_parse_unknown_attributes(request, config)
    extra_attributes.update(unknown_attributes)

    event = Event(timestamp=ts, desc=desc, tags=tags, extra_attributes=extra_attributes)
    return event


# make these functions available to plugins:
__builtin__.add_post_validate_and_parse_base_attributes = add_post_validate_and_parse_base_attributes
__builtin__.add_post_validate_and_parse_extra_attributes = add_post_validate_and_parse_extra_attributes
__builtin__.add_post_validate_and_parse_unknown_attributes = add_post_validate_and_parse_unknown_attributes
__builtin__.add_post_handler_default = add_post_handler_default


@route('/events/add', method='POST')
@route('/events/add/<handler>', method='POST')
def events_add_post(handler='default'):
    print 'IM IN HERE'
    try:
        event = call_func('add_post_handler_' + handler, request, config)
    except Exception, e:
        import traceback
        print "Could not create new event because %s: %s. Go back to previous page to retry" % (sys.exc_type, sys.exc_value)
        print 'Stacktrace:'
        traceback.print_tb(sys.exc_traceback)
        # TODO: if user came from a /events/add/<foo> page, customized for specific
        # use case, we should bring him/her back.
        # option 1: figure out the original args used to compile the template
        # (seems a bit messy to track that), but add error and form contents
        # option 2: http redirect to request.fullpath, use session for contents of form and errors
        # so that new pageload can use both.  this seems pretty feasible.
        # go to main page for now..
        return main(errors=[('Could not create new event. Go back to previous page to retry', e)])
    try:
        backend.add_event(event)
        time.sleep(1)
    except Exception, e:
        return main(errors=[('Could not save new event. Go back to previous page to retry', e)])
    return render_last_page(['/events/add', '/events/add/%s' % handler], successes=['The new event was added into the database'])


@route('/events/add/script', method='POST')
def events_add_script():
    try:
        # explicitly do the work of add_post_verify_and_parse_base_attributes
        ts = int(request.forms.event_timestamp)
        desc = request.forms.event_desc
        tags = request.forms.event_tags.split()

        # get extra and unknown attributes
        extra_attributes = add_post_validate_and_parse_extra_attributes(request, config)
        unknown_attributes = add_post_validate_and_parse_unknown_attributes(request, config)
        extra_attributes.update(unknown_attributes)

        event = Event(timestamp=ts,
                      desc=desc,
                      tags=tags,
                      extra_attributes=extra_attributes)
    except Exception, e:
        response.status = 400
        return 'Could not create new event: %s' % e
    try:
        event_id = backend.add_event(event)
        response.status = 201
        return 'ok event_id=%s\n' % event_id
    except Exception, e:
        response.status = 500
        return 'Could not save new event: %s. Go back to previous page to retry' % e

@route('/session', method='POST')
def set_session():
    print "SESSION"
    user = request.forms['session']
    #response.delete_cookie("user")
    response.set_cookie("user", user)
    del request.forms['session']
    print "User %s" % request.get_cookie("user")
    return user 
    
## HARD-CODING A LIGHT Slack EXTENSION
## when Datawarehouse repo gets installed on scratch server, replace with a call to SlackConnector()

def notify_on_close(d):
    """ sends notification msg to Slack room"""

    # put auth key on scratch server
    with open('slack_config.private') as fp:
        settings = json.load(fp)
        auth_token = settings['auth_token']

    slack = Slacker(auth_token)
    room = '#datascience-robots'
    user = d['owner']
    if request.get_cookie("user"):
        user = request.get_cookie("user")

    attachments = [{
                "fallback": "Build Failure closed",
                "pretext": ' ',
                "title": "%s failure on %s resolved by %s" % (d['job'], d['host'], user),
                "text": 'resolution: %s' % d['resolution'],
                "color": "good"
    }]

    data = json.dumps(attachments)

    try:
        # notify room
        slack.chat.post_message(room, ' ', attachments=data, username='AnthraciteBot')
    except:
        print "Failed to Nofity Slack room on event close"




@route('/report')
def report(**kwargs):
    import time
    start = local_datepick_to_unix_timestamp(config.opsreport_start)
    return p(page='report', body=template('tpl/report', config=config, reportpoints=get_report_data(start, int(time.time()))), **kwargs)


def get_report_data(start, until):
    events = backend.get_outage_events()
    # see report.tpl for definitions
    # this simple model ignores overlapping outages!
    tttf = 0
    tttd = 0
    tttr = 0
    age = 0  # time spent since start
    last_failure = start
    reportpoints = []
    # TODO there's some assumptions on tag order and such. if your events are
    # badly tagged, things could go wrong.
    # TODO honor start/until
    origin_event = Event(start, "start", [])
    reportpoints.append(Reportpoint(origin_event, 0, 100, 0, tttf, 0, tttd, 0, tttr))
    outages_seen = {}
    for event in events:
        if event.timestamp > until:
            break
        age = float(event.timestamp - start)
        ttd = 0
        ttr = 0
        if 'start' in event.tags:
            outages_seen[event.outage] = {'start': event.timestamp}
            ttf = event.timestamp - last_failure
            tttf += ttf
            last_failure = event.timestamp
        elif 'detected' in event.tags:
            ttd = event.timestamp - outages_seen[event.outage]['start']
            tttd += ttd
            outages_seen[event.outage]['ttd'] = ttd
        elif 'resolved' in event.tags:
            ttd = outages_seen[event.outage]['ttd']
            ttr = event.timestamp - outages_seen[event.outage]['start']
            tttr += ttr
            outages_seen[event.outage]['ttr'] = ttr
        else:
            # the outage changed impact. for now just ignore this, cause we
            # don't do anything with impact yet.
            pass
        muptime = float(age - tttr) * 100 / age
        reportpoints.append(Reportpoint(event, len(outages_seen), muptime, ttf, tttf, ttd, tttd, ttr, tttr))

    age = until - start
    end_event = Event(until, "end", [])
    muptime = float(age - tttr) * 100 / age
    reportpoints.append(Reportpoint(end_event, len(outages_seen), muptime, 0, tttf, 0, tttd, 0, tttr))
    return reportpoints


@route('/report/data/<catchall:re:.*>')
def report_data(catchall):
    response.content_type = 'application/x-javascript'
    start = int(request.query['from'])
    until = int(request.query['until'])
    jsonp = request.query['jsonp']
    reportpoints = get_report_data(start, until)
    data = [
        {
            "target": "ttd",
            "datapoints": [[r.ttd / 60, r.event.timestamp] for r in reportpoints]
        },
        {
            "target": "ttr",
            "datapoints": [[r.ttr / 60, r.event.timestamp] for r in reportpoints]
        }
    ]
    return '%s(%s)' % (jsonp, json.dumps(data))


@route('<path:re:/assets/.*>')
def static(path):
    return static_file(path, root='.')


@error(404)
def error404(code, **kwargs):
    return p(body=template('tpl/error', title='404 page not found', msg='The requested page was not found'), **kwargs)


def p(**kwargs):
    return page(config, backend, state, **kwargs)

app_dir = os.path.dirname(__file__)
if app_dir:
    os.chdir(app_dir)

import config
config = Config(config)
backend = Backend(config)
state = {}
(state, errors) = load_plugins(config.plugins, config)
if errors:
    for e in errors:
        sys.stderr.write(str(e))
    sys.exit(2)
session_opts = {
    'session.type': 'file',
    'session.cookie_expires': 300,
    'session.data_dir': './session_data',
    'session.auto': True
}
application = app = SessionMiddleware(app(), session_opts)
if __name__ == '__main__':
    debug(True)
    run(app=app, reloader=True, host=config.listen_host, port=config.listen_port)
