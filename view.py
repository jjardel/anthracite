from bottle import template


def page(config, backend, **kwargs):
    # accepts lists of warnings, errors, infos, successes for which we'll show
    # blocks
    kwargs['events_count'] = backend.get_events_count()
    return template('tpl/page', kwargs)


def page_light(config, backend, **kwargs):
    kwargs['events_count'] = backend.get_events_count()
    return template('tpl/page_light', kwargs)
