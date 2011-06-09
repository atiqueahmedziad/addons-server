from django import http
from django.conf import settings
from django.views.decorators.cache import never_cache
from django.views.decorators.http import condition

import commonware.log
import jingo
import waffle

from access import acl
from amo.decorators import json_view
from amo.urlresolvers import reverse
from amo.utils import HttpResponseSendFile, Message, Token
from files.decorators import (etag, file_view, compare_file_view,
                              file_view_token, last_modified)
from files.tasks import extract_file

from tower import ugettext as _


log = commonware.log.getLogger('z.addons')


def setup_viewer(request, file_obj):
    data = {'file': file_obj,
            'version': file_obj.version,
            'addon': file_obj.version.addon,
            'status': False,
            'selected': {}}

    if acl.action_allowed(request, 'Editors', '%'):
        data['file_link'] = {'text': _('Back to review'),
                             'url': reverse('editors.review',
                                            args=[data['addon'].slug])}
    else:
        data['file_link'] = {'text': _('Back to addon'),
                             'url': reverse('addons.detail',
                                            args=[data['addon'].pk])}
    return data


@never_cache
@json_view
@file_view
def poll(request, viewer):
    return {'status': viewer.is_extracted(),
            'msg': [Message('file-viewer:%s' % viewer).get(delete=True)]}


@file_view
@condition(etag_func=etag, last_modified_func=last_modified)
def browse(request, viewer, key=None, type='file'):
    data = setup_viewer(request, viewer.file)
    data['viewer'] = viewer
    data['poll_url'] = reverse('files.poll', args=[viewer.file.id])

    if (not waffle.switch_is_active('delay-file-viewer') and
        not viewer.is_extracted()):
        extract_file(viewer)

    if viewer.is_extracted():
        data.update({'status': True, 'files': viewer.get_files()})
        key = viewer.get_default(key)
        if key not in data['files']:
            raise http.Http404

        viewer.select(key)
        data['key'] = key
        if (not viewer.is_directory() and not viewer.is_binary()):
            data['content'] = viewer.read_file()

    else:
        extract_file.delay(viewer)

    tmpl = ('files/content.html' if type == 'fragment'
                                 else 'files/viewer.html')
    return jingo.render(request, tmpl, data)


@never_cache
@compare_file_view
@json_view
def compare_poll(request, diff):
    msgs = []
    for f in (diff.left, diff.right):
        m = Message('file-viewer:%s' % f).get(delete=True)
        if m:
            msgs.append(m)
    return {'status': diff.is_extracted(), 'msg': msgs}


@compare_file_view
@condition(etag_func=etag, last_modified_func=last_modified)
def compare(request, diff, key=None, type='file'):
    data = setup_viewer(request, diff.left.file)
    data['diff'] = diff
    data['poll_url'] = reverse('files.compare.poll',
                               args=[diff.left.file.id,
                                     diff.right.file.id])

    if (not waffle.switch_is_active('delay-file-viewer')
        and not diff.is_extracted()):
        extract_file(diff.left)
        extract_file(diff.right)

    if diff.is_extracted():
        data.update({'status': True,
                     'files': diff.get_files(),
                     'files_deleted': diff.get_deleted_files()})
        key = diff.left.get_default(key)
        if key not in data['files'] and key not in data['files_deleted']:
            raise http.Http404

        diff.select(key)
        data['key'] = key
        if diff.is_diffable():
            data['left'], data['right'] = diff.read_file()

    else:
        extract_file.delay(diff.left)
        extract_file.delay(diff.right)

    tmpl = ('files/content.html' if type == 'fragment'
                                 else 'files/viewer.html')
    return jingo.render(request, tmpl, data)


@file_view
def redirect(request, viewer, key):
    new = Token(data=[viewer.file.id, key])
    new.save()
    url = '%s%s?token=%s' % (settings.STATIC_URL,
                             reverse('files.serve', args=[viewer, key]),
                             new.token)
    return http.HttpResponseRedirect(url)


@file_view_token
def serve(request, viewer, key):
    """
    This is to serve files off of st.a.m.o, not standard a.m.o. For this we
    use token based authentication.
    """
    files = viewer.get_files()
    obj = files.get(key)
    if not obj:
        log.error(u'Couldn\'t find %s in %s (%d entries) for file %s' %
                  (key, files.keys()[:10], len(files.keys()), viewer.file.id))
        raise http.Http404()
    return HttpResponseSendFile(request, obj['full'],
                                content_type=obj['mimetype'])
