import logging
import os
import pickle

import bs4
import requests

ANITYA_URL = 'https://release-monitoring.org/'

from fedora.client import AuthError

log = logging.getLogger('fedmsg')

backends = {
    'ftp.debian.org': 'Debian project',
    'drupal.org': 'Drupal7',
    'freecode.com': 'Freshmeat',
    'github.com': 'Github',
    'download.gnome.org': 'GNOME',
    'ftp.gnu.org': 'GNU project',
    'code.google.com': 'Google code',
    'hackage.haskell.org': 'Hackage',
    'launchpad.net': 'launchpad',
    'npmjs.org': 'npmjs',
    'packagist.org': 'Packagist',
    'pear.php.net': 'PEAR',
    'pecl.php.net': 'PECL',
    'pypi.python.org': 'PyPI',
    'rubygems.org': 'Rubygems',
    'sourceforge.net': 'Sourceforge',
}

prefixes = [
    'drupal7-',
    'drupal6-',
    'ghc-',
    'nodejs-',
    'php-pear-',
    'php-pecl-',
    'php-',
    'python-',
    'rubygem-',
]

easy_guesses = [
    'Debian project',
    'Drupal7',
    'Freshmeat',
    'Github',
    'GNOME',
    'GNU project',
    'Google code',
    'Hackage',
    'launchpad',
    'npmjs',
    'PEAR',
    'PECL',
    'PyPI',
    'Rubygems',
]


class AnityaException(Exception):
    pass


class AnityaAuthException(AnityaException, AuthError):
    pass


def _parse_service_form(response):
    parsed = bs4.BeautifulSoup(response.text)
    inputs = {}
    for child in parsed.form.find_all(name='input'):
        if child.attrs['type'] == 'submit':
            continue
        inputs[child.attrs['name']] = child.attrs['value']
    return (parsed.form.attrs['action'], inputs)


class Anitya(object):

    def __init__(self, url=ANITYA_URL, insecure=False, cookies=None,
                 login_callback=None, login_attempts=3,
                 sessionfile="~/.cache/anitya-session.pickle"):

        self.url = url
        self.session = requests.session()
        self.insecure = insecure
        self.username = None
        self.password = None
        self.login_callback = login_callback
        self.login_attempts = login_attempts
        self.sessionfile = os.path.expanduser(sessionfile)

        try:
            with open(self.sessionfile, "rb") as sessionfo:
                self.session.cookies = pickle.load(sessionfo)["cookies"]
        except (IOError, KeyError, TypeError):
            pass

    def __send_request(self, url, method, params=None, data=None):
        log.debug(
            'Calling: %s with arg: %s and data: %s', url, params, data)
        req = self.session.request(
            method=method,
            url=url,
            params=params,
            data=data,
            verify=not self.insecure,
        )
        self._save_cookies()
        return req

    def _save_cookies(self):
        try:
            with open(self.sessionfile, 'rb') as sessionfo:
                data = pickle.load(sessionfo)
        except:
            data = {}
        try:
            with open(self.sessionfile, 'wb', 0600) as sessionfo:
                sessionfo.seek(0)
                data["cookies"] = self.session.cookies
                pickle.dump(data, sessionfo)
        except:
            pass

    @property
    def is_logged_in(self):
        response = self.session.get(self.url + '/login/fedora')
        return "logout" in response.text

    def login(self, username=None, password=None, openid_insecure=False,
              response=None):

        log.info("Attempting to login to anitya")

        if not username:
            username = self.username
        if not password:
            password = self.password
        if self.login_callback and not password:
            username, password = self.login_callback(username=username,
                                                     bad_password=False)

        if not username or not password:
            raise AnityaAuthException('Username or password missing')

        import re
        from urlparse import urlparse, parse_qs

        fedora_openid_api = r'https://id.fedoraproject.org/api/v1/'
        fedora_openid = r'^http(s)?:\/\/id\.(|stg.|dev.)?fedoraproject'\
            '\.org(/)?'
        motif = re.compile(fedora_openid)

        # Log into the service
        if not response:
            response = self.session.get(self.url + '/login/fedora')

        openid_url = ''
        if '<title>OpenID transaction in progress</title>' \
                in response.text:
            # requests.session should hold onto this for us....
            openid_url, data = _parse_service_form(response)
            if not motif.match(openid_url):
                raise AnityaException(
                    'Un-expected openid provider asked: %s' % openid_url)
        elif 'logged in as' in response.text:
            # User already logged in via its cookie file by default:
            # ~/.cache/anitya-session.pickle
            return
        else:
            data = {}
            for resp in response.history:
                if motif.match(resp.url):
                    parsed = parse_qs(urlparse(resp.url).query)
                    for key, value in parsed.items():
                        data[key] = value[0]
                    break
            else:
                log.info(response.text)
                raise AnityaException(
                    'Unable to determine openid parameters from login: %r' %
                    openid_url)

        # Contact openid provider
        data['username'] = username
        data['password'] = password
        # Let's precise to FedOAuth that we want to authenticate with FAS
        data['auth_module'] = 'fedoauth.auth.fas.Auth_FAS'

        response = self.__send_request(
            url=fedora_openid_api,
            method='POST',
            data=data)
        output = response.json()

        if not output['success']:
            raise AnityaException(output['message'])

        response = self.__send_request(
            url=output['response']['openid.return_to'],
            method='POST',
            data=output['response'])

        return output

    def search(self, name, homepage):
        url = '{0}/api/projects/?homepage={1}'.format(self.url, homepage)
        log.info("Looking for %r via %r" % (name, url))
        response = self.__send_request(url, method='GET')
        return response.json()

    def map_new_package(self, name, project):
        if not self.is_logged_in:
            log.error('Could not add new anitya project.  Not logged in.')
            return False

        idx = project['id']
        url = self.url + '/project/%i/map' % idx
        response = self.__send_request(url, method='GET')
        if not response.status_code == 200:
            code = response.status_code
            log.error("Couldn't get form page to get csrf token %r" % code)
            return False

        soup = bs4.BeautifulSoup(response.text)
        data = dict(
            distro='Fedora',
            package_name=name,
            csrf_token=soup.form.find(id='csrf_token').attrs['value'],
        )
        response = self.__send_request(url, method='POST', data=data)

        if not response.status_code == 200:
            code = response.status_code
            log.error('Failed to map in anitya, status %r: %r' % (code, data))
            return False
        elif 'Could not' in response.text:
            log.error('Failed to map in anitya, validation failure: %r' % data)
            return False
        else:
            log.info('Successfully mapped %r in anitya' % name)
            return True

    def add_new_project(self, name, homepage):
        if not self.is_logged_in:
            log.error('Could not add new anitya project.  Not logged in.')
            return False

        data = dict(
            name=name,
            homepage=homepage,
            distro='Fedora',
            package_name=name,
        )

        # Try to guess at what backend to prefill...
        for target, backend in backends.items():
            if target in homepage:
                data['backend'] = backend
                break

        if 'backend' not in data:
            log.error('Could not determine backend for %r' % homepage)
            return False

        # It's not always the case that these need removed, but often
        # enough...
        for prefix in prefixes:
            if data['name'].startswith(prefix):
                data['name'] = data['name'][len(prefix):]

        # For these, we can get a pretty good guess at the upstream name
        for guess in easy_guesses:
            if data['backend'] == guess:
                data['name'] = data['homepage'].strip('/').split('/')[-1]
                break

        url = self.url + '/project/new'
        response = self.__send_request(url, method='GET')

        if not response.status_code == 200:
            code = response.status_code
            log.error("Couldn't get form page to get csrf token %r" % code)
            return False

        soup = bs4.BeautifulSoup(response.text)
        data['csrf_token'] = soup.form.find(id='csrf_token').attrs['value']

        response = self.__send_request(url, method='POST', data=data)

        if not response.status_code == 200:
            log.error('Failed to add to anitya: %r' % data)
            return False
        elif 'Could not' in response.text:
            log.error('Failed to add to anitya: %r' % data)
            return False
        else:
            log.info('Successfully added %r to anitya' % data['name'])
            return True
