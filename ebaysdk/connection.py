# -*- coding: utf-8 -*-

'''
Copyright 2012-2019 eBay Inc.
Authored by: Tim Keefer
Licensed under CDDL 1.0
'''
import logging

from ebaysdk import log

import re
import time
import uuid
import webbrowser

from requests import Request, Session
from requests.adapters import HTTPAdapter

from xml.dom.minidom import parseString
from xml.parsers.expat import ExpatError

from ebaysdk import set_stream_logger, UserAgent
from ebaysdk.utils import getNodeText as getNodeTextUtils, smart_encode, smart_decode
from ebaysdk.utils import getValue, smart_encode_request_data
from ebaysdk.response import Response
from ebaysdk.exception import ConnectionError, ConnectionResponseError

import requests, base64
from requests.structures import CaseInsensitiveDict
import json
import datetime
from datetime import datetime


HTTP_SSL = {
    False: 'http',
    True: 'https',
}

class TokenManager():

    URL_SANDBOX = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    URL_PRODUCTION = "https://api.ebay.com/identity/v1/oauth2/token"
    TOKEN_TTL_SECS = 60 * 60

    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.logger = logging.getLogger(name='EbayTokenManager')

        self.__current_token = None
        self.__date_token_fetched = None

    def get_token(self):
        if not self.__current_token or self.is_expired():
            self.logger.info('Getting api token from ebay ....')
            self.__current_token = self._send_request()
            self.__date_token_fetched = datetime.now()
            self.logger.info('...token retreived!')
            return self.__current_token
        else:
            return self.__current_token

    def is_expired(self):
        diff = datetime.now() - self.__date_token_fetched
        expired = diff.seconds >= self.TOKEN_TTL_SECS
        return expired

    def _send_request(self):
        '''
        @see https://github.com/timotheus/ebaysdk-python/issues/347
        '''
        url = self.URL_PRODUCTION

        credentials = f"{self.client_id}:{self.client_secret}"
        credentials_bytes = credentials.encode('ascii')
        base64_bytes = base64.b64encode(credentials_bytes)
        credentials_b64 = base64_bytes.decode('ascii')

        headers = CaseInsensitiveDict()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Authorization"] = f"Basic {credentials_b64}"
        data = "grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope"
        response = requests.post(url, headers=headers, data=data)
        response_dict = response.json()
        access_token = response_dict['access_token']
        return access_token

class BaseConnection(object):
    """Base Connection Class."""

    def __init__(self, debug=False, method='GET',
                 proxy_host=None, timeout=20, proxy_port=80,
                 parallel=None, escape_xml=False, **kwargs):

        # retreive mandatory client_id and client_secret
        kwargs = kwargs or {}
        client_id = kwargs.get('client_id')
        client_secret = kwargs.get('client_secret')

        if not client_id or not client_secret:
            raise ValueError('<client_id> and <client_secret> must be provided for API access')

        self.token_manager = TokenManager(client_id=client_id , client_secret=client_secret)

        if debug:
            set_stream_logger()

        self.response = None
        self.request = None
        self.verb = None
        self.config = None
        self.debug = debug
        self.method = method
        self.timeout = timeout
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.escape_xml = escape_xml
        self.datetime_nodes = []
        self._list_nodes = []

        self.proxies = dict()
        if self.proxy_host:
            proxy = 'http://%s:%s' % (self.proxy_host, self.proxy_port)
            self.proxies = {
                'http': proxy,
                'https': proxy
            }

        self.session = Session()
        self.session.mount('http://', HTTPAdapter(max_retries=3))
        self.session.mount('https://', HTTPAdapter(max_retries=3))

        self.parallel = parallel

        self.base_list_nodes = []
        self.datetime_nodes = []

        self._reset()

    def debug_callback(self, debug_type, debug_message):
        log.debug('type: ' + str(debug_type) + ' message' + str(debug_message))

    def v(self, *args, **kwargs):
        return getValue(self.response.dict(), *args, **kwargs)

    def getNodeText(self, nodelist):
        return getNodeTextUtils(nodelist)

    def _reset(self):
        self.response = None
        self.request = None
        self.verb = None
        self._list_nodes = []
        self._request_id = None
        self._request_dict = {}
        self._time = time.time()
        self._response_content = None
        self._response_dom = None
        self._response_obj = None
        self._response_soup = None
        self._response_dict = None
        self._response_error = None
        self._resp_body_errors = []
        self._resp_body_warnings = []
        self._resp_codes = []

    def _add_prefix(self, nodes, verb):
        if verb:
            for i, v in enumerate(nodes):
                if not nodes[i].startswith(verb.lower()):
                    nodes[i] = "%sresponse.%s" % (
                        verb.lower(), nodes[i].lower())

    def execute(self, verb, data=None, list_nodes=[], verb_attrs=None, files=None):
        "Executes the HTTP request."
        log.debug('execute: verb=%s data=%s' % (verb, data))

        self._reset()

        self._list_nodes += list_nodes
        self._add_prefix(self._list_nodes, verb)

        if hasattr(self, 'base_list_nodes'):
            self._list_nodes += self.base_list_nodes

        self.build_request(verb, data, verb_attrs, files)
        self.execute_request()

        if hasattr(self.response, 'content'):
            self.process_response()
            self.error_check()

        log.debug('total time=%s' % (time.time() - self._time))

        return self.response

    def build_request(self, verb, data, verb_attrs, files=None):

        self.verb = verb
        self._request_dict = data
        self._request_id = uuid.uuid4()

        url = self.build_request_url(verb)

        headers = self.build_request_headers(verb)
        headers.update({'User-Agent': UserAgent,
                        'X-EBAY-SDK-REQUEST-ID': str(self._request_id)})

        token = self.token_manager.get_token()
        headers.update({'X-EBAY-API-IAF-TOKEN':token})

        # if we are adding files, we ensure there is no Content-Type header already defined
        # otherwise Request will use the existing one which is likely not to be multipart/form-data
        # data must also be a dict so we make it so if needed

        requestData = self.build_request_data(verb, data, verb_attrs)
        if files:
            del(headers['Content-Type'])
            if isinstance(requestData, str):  # pylint: disable-msg=E0602
                requestData = {'XMLPayload': requestData}

        request = Request(self.method,
                          url,
                          data=smart_encode_request_data(requestData),
                          headers=headers,
                          files=files,
                          )

        self.request = request.prepare()

    def build_request_headers(self, verb):
        return {}

    def build_request_data(self, verb, data, verb_attrs):
        return ""

    def build_request_url(self, verb):
        url = "%s://%s%s" % (
            HTTP_SSL[self.config.get('https', True)],
            self.config.get('domain'),
            self.config.get('uri')
        )
        return url

    def execute_request(self):

        log.debug("REQUEST (%s): %s %s"
                  % (self._request_id, self.request.method, self.request.url))
        log.debug('headers=%s' % self.request.headers)
        log.debug('body=%s' % self.request.body)

        if self.parallel:
            self.parallel._add_request(self)
            return None

        self.response = self.session.send(self.request,
                                          verify=True,
                                          proxies=self.proxies,
                                          timeout=self.timeout,
                                          allow_redirects=True
                                          )

        log.debug('RESPONSE (%s):' % self._request_id)
        log.debug('elapsed time=%s' % self.response.elapsed)
        log.debug('status code=%s' % self.response.status_code)
        log.debug('headers=%s' % self.response.headers)
        log.debug('content=%s' % self.response.text)

    def process_response(self, parse_response=True):
        """Post processing of the response"""

        self.response = Response(self.response,
                                 verb=self.verb,
                                 list_nodes=self._list_nodes,
                                 datetime_nodes=self.datetime_nodes,
                                 parse_response=parse_response)

        self.session.close()
        # set for backward compatibility
        self._response_content = self.response.content

        if self.response.status_code != 200:
            self._response_error = self.response.reason

    def error_check(self):
        estr = self.error()

        if estr and self.config.get('errors', True):
            log.error(estr)
            raise ConnectionError(estr, self.response)

    def response_codes(self):
        return self._resp_codes

    def response_status(self):
        "Retuns the HTTP response status string."

        return self.response.reason

    def response_code(self):
        "Returns the HTTP response status code."

        return self.response.status_code

    def response_content(self):
        return self.response.content

    def response_soup(self):
        "Returns a BeautifulSoup object of the response."

        if not self._response_soup:
            try:
                from bs4 import BeautifulStoneSoup
            except ImportError:
                from BeautifulSoup import BeautifulStoneSoup
                log.warn(
                    'DeprecationWarning: BeautifulSoup 3 or earlier is deprecated; install bs4 instead\n')

            self._response_soup = BeautifulStoneSoup(
                smart_decode(self.response_content)
            )

        return self._response_soup

    def response_obj(self):
        log.warn('response_obj() DEPRECATED, use response.reply instead')
        return self.response.reply

    def response_dom(self):
        """ Deprecated: use self.response.dom() instead
        Returns the response DOM (xml.dom.minidom).
        """
        log.warn('response_dom() DEPRECATED, use response.dom instead')

        if not self._response_dom:
            dom = None
            content = None

            try:
                if self.response.content:
                    regex = re.compile(b'xmlns="[^"]+"')
                    content = regex.sub(b'', self.response.content)
                else:
                    content = "<%sResponse></%sResponse>" % (
                        self.verb, self.verb)

                dom = parseString(content)
                self._response_dom = dom.getElementsByTagName(
                    self.verb + 'Response')[0]

            except ExpatError as e:
                raise ConnectionResponseError(
                    "Invalid Verb: %s (%s)" % (self.verb, e), self.response)
            except IndexError:
                self._response_dom = dom

        return self._response_dom

    def response_dict(self):
        "Returns the response dictionary."
        log.warn(
            'response_dict() DEPRECATED, use response.dict() or response.reply instead')

        return self.response.reply

    def response_json(self):
        "Returns the response JSON."
        log.warn('response_json() DEPRECATED, use response.json() instead')

        return self.response.json()

    def _get_resp_body_errors(self):
        """Parses the response content to pull errors.

        Child classes should override this method based on what the errors in the
        XML response body look like. They can choose to look at the 'ack',
        'Errors', 'errorMessage' or whatever other fields the service returns.
        the implementation below is the original code that was part of error()
        """

        if self._resp_body_errors and len(self._resp_body_errors) > 0:
            return self._resp_body_errors

        errors = []

        if self.verb is None:
            return errors

        dom = self.response.dom()
        if dom is None:
            return errors

        return []

    def error(self):
        "Builds and returns the api error message."

        error_array = []
        if self._response_error:
            error_array.append(self._response_error)

        error_array.extend(self._get_resp_body_errors())

        if len(error_array) > 0:
            # Force all errors to be unicode in a proper way
            error_array = [smart_decode(smart_encode(e)) for e in error_array]
            error_string = u"{verb}: {message}".format(
                verb=self.verb, message=u", ".join(error_array))

            return error_string

        return None

    def opendoc(self):
        webbrowser.open(self.config.get('doc_url'))
