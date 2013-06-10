#!/usr/bin/env python
# Copyright 2013 Brett Slatkin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implements authentication for the API server and frontend."""

import functools
import json
import logging
import urllib
import urllib2

# Local libraries
from flask import abort, redirect, render_template, request, url_for
from flask.ext.login import (
    current_user, fresh_login_required, login_required, login_user)

# Local modules
from . import app
from . import db
from . import login
import config
import forms
import models


GOOGLE_OAUTH2_AUTH_URL = 'https://accounts.google.com/o/oauth2/auth'
GOOGLE_OAUTH2_TOKEN_URL = 'https://accounts.google.com/o/oauth2/token'
GOOGLE_OAUTH2_USERINFO_URL = 'https://www.googleapis.com/oauth2/v1/userinfo'
GOOGLE_OAUTH2_SCOPES ='https://www.googleapis.com/auth/userinfo.email'
FETCH_TIMEOUT_SECONDS = 60


@login.user_loader
def load_user(user_id):
    return models.User.query.get(user_id)


@app.route('/login')
def login_view():
    # Inspired by:
    #   http://stackoverflow.com/questions/9499286
    #   /using-google-oauth2-with-flask
    params = dict(
        response_type='code',
        client_id=config.GOOGLE_OAUTH2_CLIENT_ID,
        redirect_uri=config.GOOGLE_OAUTH2_REDIRECT_URI,
        scope=GOOGLE_OAUTH2_SCOPES,
        state=request.args.get('next'),
    )
    target_url = '%s?%s' % (
        GOOGLE_OAUTH2_AUTH_URL, urllib.urlencode(params))
    logging.debug('Redirecting url=%r', target_url)
    return redirect(target_url)


@app.route(config.GOOGLE_OAUTH2_REDIRECT_PATH)
def login_auth():
    # TODO: Handle when the 'error' parameter is present
    params = dict(
        code=request.args.get('code'),
        client_id=config.GOOGLE_OAUTH2_CLIENT_ID,
        client_secret=config.GOOGLE_OAUTH2_CLIENT_SECRET,
        redirect_uri=config.GOOGLE_OAUTH2_REDIRECT_URI,
        grant_type='authorization_code'
    )
    payload = urllib.urlencode(params)
    logging.debug('Posting url=%r, payload=%r',
                  GOOGLE_OAUTH2_TOKEN_URL, payload)
    fetch_request = urllib2.Request(GOOGLE_OAUTH2_TOKEN_URL, payload)
    conn = urllib2.urlopen(fetch_request, timeout=FETCH_TIMEOUT_SECONDS)
    data = conn.read()
    result_dict = json.loads(data)

    params = dict(
        access_token=result_dict['access_token']
    )
    payload = urllib.urlencode(params)
    target_url = '%s?%s' % (GOOGLE_OAUTH2_USERINFO_URL, payload)
    logging.debug('Fetching url=%r', target_url)
    fetch_request = urllib2.Request(target_url)
    conn = urllib2.urlopen(fetch_request, timeout=FETCH_TIMEOUT_SECONDS)
    data = conn.read()
    result_dict = json.loads(data)

    user_id = '%s:%s' % (models.User.GOOGLE_OAUTH2, result_dict['id'])
    user = models.User.query.get(user_id)
    if not user:
        user = models.User(
            id=user_id,
            email_address=result_dict['email'])
        db.session.add(user)
        db.session.commit()

    login_user(user)

    return redirect(request.args.get('state'))


@app.route('/whoami')
@login_required
def debug_login():
    context = {
        'user': current_user,
    }
    return render_template('whoami.html', **context)


def superuser_required(f):
    """Requires the requestor to be a super user."""
    @functools.wraps(f)
    @login_required
    def wrapped(*args, **kwargs):
        if not (current_user.is_authenticated() and current_user.superuser):
            return abort(403)
        return f(*args, **kwargs)
    return wrapped


def can_user_access_build(param_name):
    """Determines if the current user can access the build ID in the request.

    Args:
        param_name: Parameter name to use for getting the build ID from the
            request. Will fetch from GET or POST requests.

    Returns:
        Tuple (build, response) where response is None if the user has
        access to the build, otherwise it is a flask.Response object to
        return to the user describing the error.
    """
    build_id = (
        request.args.get(param_name, type=int) or
        request.form.get(param_name, type=int))
    if not build_id:
        logging.debug('Build ID in param_name=%r was missing', param_name)
        return abort(400), None

    build = models.Build.query.get(build_id)
    if not build:
        logging.debug('Could not find build_id=%r', build_id)
        return abort(404), None

    user_is_owner = False

    if current_user.is_authenticated():
        user_is_owner = build.owners.filter_by(
            id=current_user.get_id()).first()

    if not user_is_owner:
        if request.method != 'GET':
            logging.debug('No way to log in user via modifying request')
            return abort(403), None
        elif build.public:
            pass
        elif current_user.is_authenticated():
            logging.debug('User must authenticate to see non-public build')
            return abort(403), None
        else:
            logging.debug('Redirecting user to login to get build access')
            return login.unauthorized(), None

    return None, build


def build_access_required(function_or_param_name):
    """Decorator ensures user has access to the build ID in the request.

    May be used in two ways:

        @build_access_required
        def my_func(build):
            ...

        @build_access_required('custom_build_id_param')
        def my_func(build):
            ...

    Always calls the given function with the models.Build entity as the
    first positional argument.
    """
    def get_wrapper(param_name, f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            response, build = can_user_access_build(param_name)
            if response:
                return response
            else:
                return f(build, *args, **kwargs)
        return wrapper

    if isinstance(function_or_param_name, basestring):
        return lambda f: get_wrapper(function_or_param_name, f)
    else:
        return get_wrapper('id', function_or_param_name)


def api_key_required(f):
    """Decorator ensures API key has proper access to requested resources."""
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        auth_header = request.authorization
        if not auth:
            logging.debug('API request lacks authorization header')
            return flask.Response(
                'API key required', 401,
                {'WWW-Authenticate', 'Basic realm="API key required"'})

        api_key = models.ApiKey.query.get(auth_header.username)
        if not api_key:
            logging.debug('API key=%r does not exist', auth_header.username)
            return abort(403)

        if not api_key.active:
            logging.debug('API key=%r is no longer active', api_key.id)
            return abort(403)

        if api_key.secret != auth_header.password:
            logging.debug('API key=%r password does not match', api_key.id)
            return abort(403)

        logging.debug('Authenticated as API key=%r', api_key.id)

        build_id = request.form.get('build_id', type=int)

        if not api_key.superuser:
            if build_id and api_key.build_id == build_id:
                # Only allow normal users to edit builds that exist.
                build = models.Build.query.get(build_id)
                if not build:
                    logging.debug('API key=%r accessing missing build_id=%r',
                                  api_key.id, build_id)
                    return abort(404)
            else:
                logging.debug('API key=%r cannot access requested build_id=%r',
                              api_key.id, build_id)
                return abort(403)

        return f(*args, **kwargs)

    return wrapped


@app.route('/api_keys', methods=['GET', 'POST'])
@fresh_login_required
@build_access_required('build_id')
def manage_api_keys(build):
    """Page for viewing and creating API keys."""
    create_form = forms.CreateApiKeyForm()
    if create_form.validate_on_submit():
        api_key = models.ApiKey()
        create_form.populate_obj(api_key)
        api_key.id = utils.human_uuid()
        api_key.secret = utils.password_uuid()
        db.session.add(api_key)
        db.session.commit()

        logging.info('Created API key=%r for build_id=%r',
                     api_key.id, build.id)
        return redirect(url_for('manage_api_keys', build_id=build.id))

    create_form.build_id.data = build.id

    api_key_query = (
        models.ApiKey.query
        .filter_by(build_id=build.id)
        .order_by(models.ApiKey.created.desc())
        .limit(1000))

    revoke_form_list = []
    for api_key in api_key_query:
        form = forms.RevokeApiKeyForm()
        form.id.data = api_key.id
        form.build_id.data = build.id
        form.revoke.data = True
        revoke_form_list.append((api_key, form))

    return render_template(
        'view_api_keys.html',
        build=build,
        create_form=create_form,
        revoke_form_list=revoke_form_list)


@app.route('/api_keys.revoke', methods=['POST'])
@fresh_login_required
@build_access_required('build_id')
def revoke_api_key(build):
    """Form submission handler for revoking API keys."""
    form = forms.RevokeApiKeyForm()
    if form.validate_on_submit():
        api_key = models.ApiKey.query.get(form.id.data)
        if api_key.build_id != build.id:
            logging.debug('User does not have access to API key=%r',
                          api_key.id)
            return abort(403)

        api_key.active = False
        db.session.add(api_key)
        db.session.commit()

    return redirect(url_for('manage_api_keys', build_id=build.id))