import json
import os
import platform
import re
import signal
import subprocess
import urllib.request
import urllib.parse
from urllib.error import HTTPError
from deoplete.util import getlines,expand
from deoplete.source.base import Base

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

class ServerError(Exception):
    pass


class ClientError(Exception):
    pass


def post_request(url, path, params):
    url = urllib.parse.urljoin(url, path)
    params = collect_not_none(params)
    data = urllib.parse.urlencode(params).encode('ascii')

    req = opener.open(url, data)
    return req.read()


def collect_not_none(d):
    return {key: d[key] for key in d if d[key] is not None}

class Server:
    def __init__(self, command='solargraph', args=['socket']):
        self.command = command
        self.args = args
        self.proc = None
        self.port = None
        self.start()
        signal.signal(signal.SIGTERM, lambda num, stack : self.stop())
        signal.signal(signal.SIGHUP, lambda num, stack : self.stop())
        signal.signal(signal.SIGINT, lambda num, stack : self.stop())
        self.host = 'localhost'
        self.url = 'http://{}:{}/'.format(self.host, self.port)

    def start(self):
        env = os.environ.copy()
        self.proc = subprocess.Popen(
            [self.command, *self.args],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # until to get port number
        output = ''
        while True:
            line = self.proc.stdout.readline().decode('utf-8')

            if not line:
                raise ServerError('Failed to start server' + (output and ':\n' + output))

            match = re.search(r'PORT=(\d+)', line)
            if match:
                self.port = int(match.group(1))
                break

            output += line

    def stop(self):
        if self.proc is None:
            return

        self.proc.stdout.close()
        self.proc.kill()
        self.proc = None
        self.port = None

    def is_started(self):
        return self.proc is not None and self.port is not None


class Client:
    def __init__(self, url):
        self.url = url

    def request(self, path, params):
        try:
            result = post_request(self.url, path, params)
            return json.loads(result.decode('utf8'))
        except HTTPError as error:
            raise ClientError(str(error)) from error

    def prepare(workspace):
        return self.request('prepare', {'workspace': workspace})

    def update(filename, workspace=None):
        return self.request('update', {'filename': filename, 'workspace': workspace})

    def suggest(self, text, line, column, filename=None, workspace=None, with_snippets=None, with_all=None):
        params = {
            'text': text,
            'line': line,
            'column': column,
            'filename': filename,
            'workspace': workspace,
            'with_snippets': with_snippets,
            'all': with_all,
        }
        return self.request('suggest', params)

    def define(self, text, line, column, filename=None, workspace=None):
        params = {
            'text': text,
            'line': line,
            'column': column,
            'filename': filename,
            'workspace': workspace,
        }
        return self.request('define', params)

    def resolve(self, path, filename, workspace):
        params = {
            'path': path,
            'filename': filename,
            'workspace': workspace,
        }
        return self.request('resolve', params)

    def signify(self, text, line, column, filename=None, workspace=None):
        params = {
            'text': text,
            'line': line,
            'column': column,
            'filename': filename,
            'workspace': workspace,
        }
        return self.request('signify', params)

def find_dir_recursive(base_dir, targets):
    while True:
        parent = os.path.dirname(base_dir[:-1])

        if parent == '':
            return None

        for path in targets:
            if os.path.exists(os.path.join(base_dir, path)):
                return base_dir

        base_dir = parent

class Source(Base):
    def __init__(self, vim):
        Base.__init__(self, vim)
        self.name = 'solargraph'
        self.filetypes = ['ruby']
        self.mark = '[solar]'
        self.rank = 900
        self.input_pattern = r'\.[a-zA-Z0-9_?!]+|[a-zA-Z]\w*::\w*'
        self.is_server_started = False

    def on_init(self, context):
        vars = context['vars']
        self.encoding = self.vim.eval('&encoding')
        self.workspace_cache = {}

        self.command = expand(vars.get('deoplete#sources#solargraph#command', 'solargraph'))
        self.args = vars.get('deoplete#sources#solargraph#args', ['socket'])

    def start_server(self):
        if self.is_server_started == True:
            return True

        if not self.command:
            self.print_error('No solargraph binary set.')
            return

        if not self.vim.call('executable', self.command):
            return False

        try:
            self.server = Server(self.command, self.args)
        except ServerError as error:
            self.print_error(str(error))
            return False

        self.client = Client(self.server.url)
        self.is_server_started = True
        return True

    def get_complete_position(self, context):
        m = re.search('[a-zA-Z0-9_?!]*$', context['input'])
        return m.start() if m else -1

    def gather_candidates(self, context):
        if not self.start_server():
            return []

        line = context['position'][1] - 1
        column = context['complete_position']
        text = '\n'.join(getlines(self.vim)).encode(self.encoding)
        filename = context['bufpath']
        workspace = self.find_workspace_directory(context['bufpath'])

        result = self.client.suggest(text=text, line=line, column=column, filename=filename, workspace=workspace)

        if result['status'] != 'ok':
            self.print_error(result)
            return []

        output = result['suggestions']

        return [{
            'word': cand['insert'],
            'kind': cand['kind'],
            'dup': 1,
            'abbr': self.build_abbr(cand),   # in popup menu instead of 'word'
            'info': cand['label'],  # in preview window
            'menu': cand['detail'], # after 'word' or 'abbr'
        } for cand in result['suggestions']]

    def build_abbr(self, cand):
        abbr = cand['label']
        kind = cand['kind']

        if kind == 'Method':
            args = ', '.join(cand['arguments'])
            abbr += '({})'.format(args)

        return abbr

    def get_absolute_filepath(self):
        path = self.vim.call('expand', '%:p')
        if len(path) == 0:
            return None
        return path

    def find_workspace_directory(self, filepath):
        file_dir = os.path.dirname(filepath)
        if len(file_dir) == '':
            return None

        if file_dir in self.workspace_cache:
            return self.workspace_cache[file_dir]

        self.workspace_cache[file_dir] = find_dir_recursive(file_dir, ['Gemfile', '.git']) or file_dir
        return self.workspace_cache[file_dir]
