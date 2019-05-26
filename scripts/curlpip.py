#!/usr/bin/env python3

import os
import sys
import re
import subprocess
import tempfile
import json
from collections import namedtuple
from zipfile import ZipFile
import tarfile
import uuid
from distutils.version import LooseVersion

from pkg_resources import get_distribution

try:
    from pip._internal.wheel import Wheel
except:
    Wheel = None


PROJECT_API = 'https://pypi.org/pypi/%s/json'
CACHE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'cache'))

VersionRequirement = namedtuple('VersionRequirement', (
    'version', 'operator'
),
defaults=(
    None, '=='
))

InstallModule = namedtuple('InstallModule', (
    'name', 'version_requirements'
),
defaults=(
    None, []
))


class ModuleInstaller(object):
    def __init__(self):
        self._pip_path = self.get_pip_path()
        self._curl_path = self.get_curl_path()
        self._tmp_dir = None
        self._extract_dirs = {}
        self._301_filter = re.compile(r'/pypi/([\w\.\-\_]+)/json;')
        self._api_cache = {}
        self.init_cache_dir()

    def init_cache_dir(self):
        if not os.path.exists(CACHE_DIR):
            os.mkdir(CACHE_DIR)

    def get_command_result(self, cmd):
        try:
            output = subprocess.check_output(
                cmd.split(), stderr=subprocess.STDOUT)
        except Exception as e:
            output = e.output
        return output.decode(encoding='utf-8')

    def get_command_results(self, cmd):
        return self.get_command_result(cmd).splitlines()

    def get_command_path(self, cmd):
        cmd_path = self.get_command_results('which ' + cmd)
        if not cmd_path:
            return None
        return cmd_path[0]

    def get_curl_path(self):
        return self.get_command_path('curl')

    def get_pip_path(self):
        return self.get_command_path('pip')

    def get_elem(self, list_, index):
        if index < len(list_):
            return list_[index]
        return None

    def get_install_modules(self, modules):
        version_filter = re.compile('^(\w+)==(\w+)$')
        install_modules = []
        for module in modules:
            with_version = version_filter.search(module)
            if with_version:
                install_modules.append(
                    InstallModule(
                        with_version.group(1),
                        [VersionRequirement(LooseVersion(with_version.group(2)))]))
                continue
            install_modules.append(
                InstallModule(module))
        return install_modules

    def get_modules(self):
        if len(sys.argv) < 3:
            print('Error: arguments are not found.')
            sys.exit(1)

        sub_cmd = self.get_elem(sys.argv, 1)
        if sub_cmd != 'install':
            print('Error: sub command "%s" is not supported.' % sub_cmd)
            sys.exit(1)
        
        option = self.get_elem(sys.argv, 2)
        modules = None
        if option.startswith('-'):
            if option != '-r':
                print('Error: option "%s" is not supported.' % option)
                sys.exit(1)
            
            fname = self.get_elem(sys.argv, 3)
            if not fname or not os.path.isfile(fname):
                print('Error: requirements.txt is not set.')
                sys.exit(1)
            
            with open(fname, 'r') as fp:
                modules = fp.read().splitlines()
            
            if not modules:
                print('Error: No module specified in "%s".' % fname)
                sys.exit(1)
            
            return self.get_install_modules(modules)
        
        modules = sys.argv[2:]
        return self.get_install_modules(modules)

    def is_supported_wheel(self, fname):
        return Wheel(fname).supported()

    def find_module_file(self, module_objs, package_type):
        for file_info in module_objs:
            if file_info['packagetype'] != package_type:
                continue
            url = file_info['url']
            fname = file_info['filename']
            python_version = file_info['python_version']
            if package_type == 'sdist' and python_version == 'source':
                return url
            if self.is_supported_wheel(fname):
                return url
        return None

    def find_whl(self, module_objs):
        return self.find_module_file(module_objs, 'bdist_wheel')

    def find_source(self, module_objs):
        return self.find_module_file(module_objs, 'sdist')

    def compare_version(self, target_version, require_version, operator):
        if not require_version:
            return True
        
        if not operator or operator == '==':
            return target_version == require_version

        if operator == '!=':
            return target_version != require_version
        
        if operator == '>=':
            return target_version >= require_version
        
        if operator == '>':
            return target_version > require_version
        
        if operator == '<=':
            return target_version <= require_version
        
        if operator == '<':
            return target_version < require_version
        
        return False
        
    def get_project_json(self, project):
        print('  Fetching Info for ' + project)

        if project in self._api_cache:
            return self._api_cache[project]

        def get_response(module_name):
            url = PROJECT_API % module_name
            return self.get_command_result('curl -s ' + url)

        def text2json(text):
            try:
                return json.loads(text)
            except json.decoder.JSONDecodeError:
                return text

        def check_301_text(text):
            return type(text) == str and self._301_filter.search(text) is not None
        
        def check_301_json(obj):
            return type(obj) == dict and 'code' in obj and obj['code'] == '301 Moved Permanently'

        def fix_301_text(text):
            res301 = self._301_filter.search(text)
            if res301:
                text = get_response(res301.group(1))
            return text2json(text)
        
        def fix_301_json(obj):
            res301 = self._301_filter.search(obj['message'])
            if res301:
                text = get_response(obj['message'])
                return text2json(text)
            return obj

        max_count = 20
        count = 0

        text = get_response(project)
        obj = fix_301_text(text)

        while count < max_count:
            if check_301_text(obj):
                obj = fix_301_text(obj)
                count += 1
                continue
            if check_301_json(obj):
                obj = fix_301_json(obj)
                count += 1
                continue
            break
        
        response = obj if type(obj) == dict else None
        self._api_cache[project] = response
        return response

    def is_match_version(self, target_version, version_requirements):
        if not version_requirements:
            return True
        return all([
            self.compare_version(target_version, req.version, req.operator)
            for req in version_requirements
        ])

    def get_module_url(self, module):
        def sort_releases(releases):
            releases_list = [
                (LooseVersion(version), release)
                for version, release in releases.items()]
            return sorted(
                releases_list,
                key=lambda ver_release:ver_release[0],
                reverse=True)
        
        def get_url(project_obj, file_type):
            finders = {
                'whl': self.find_whl,
                'source': self.find_source,
            }

            latest_version = project_obj['info']['version']

            if self.is_match_version(LooseVersion(latest_version), module.version_requirements):
                module_objs = project_obj['releases'][latest_version]
                url = finders[file_type](module_objs)
                if url:
                    return url

            url = finders[file_type](project_obj['urls'])
            if url:
                return url

            for version, release in sort_releases(project_obj['releases']):
                if not self.is_match_version(version, module.version_requirements):
                    continue
                url = finders[file_type](release)
                if url:
                    return url
            return None

        project = self.get_project_json(module.name)

        if not project:
            print('Error: API response is wrong.')
            sys.exit(1)

        whl_url = get_url(project, 'whl')

        if whl_url:
            return whl_url

        return get_url(project, 'source')

    def is_already_installed(self, fname):
        try:
            names = fname.split('-')
            name = names[0]
            version = names[1]
            dist = get_distribution(name)
            return dist.version == version
        except:
            pass
        return False

    def download_module(self, module):
        url = self.get_module_url(module)
        if not url:
            print('    Error: Failed to get url of "%s"' % module.name)
            return None
        fname = url.split('/')[-1]
        fpath = os.path.join(CACHE_DIR, fname)
        if os.path.isfile(fpath):
            print('    Reuse archive file from cache.')
            return fpath
        cmd = 'curl -s %s -o %s' % (url, fpath)
        print('  Downloading ' + module.name)
        subprocess.run(cmd.split())
        return fpath

    def get_whl_dependencies(self, whl):
        deps_label = 'Requires-Dist:'
        module_filter = re.compile('^([\w\[\]\-]+) \(([\<\>\=\!\w\.\,]+)\)$')
        version_filter = re.compile('([\<\>\=\!]+)([\w\.]+)')

        def dep2module(dep_line):
            dep = line.replace(deps_label,'').strip()
            res = module_filter.search(dep)
            if not res:
                if all([s not in '()!=<>' for s in dep]):
                    return InstallModule(dep)
                return None
            name = res.group(1)
            if '[' in name:
                name = name.split('[')[0]
            versions = [
                version_filter.search(version)
                for version in res.group(2).split(',')
            ]
            return InstallModule(
                name, [
                    VersionRequirement(LooseVersion(v.group(2)), v.group(1))
                    for v in versions
                    if v is not None
                ]
            )

        fname = os.path.basename(whl)
        fname_values = fname.split('-')
        name = fname_values[0]
        version = fname_values[1]
        meta = '%s-%s.dist-info/METADATA' % (name, version)
        deps = []
        with ZipFile(whl) as zip:
            with zip.open(meta) as metafp:
                lines = metafp.read().decode('utf-8').splitlines()
                for line in lines:
                    if not line.startswith(deps_label):
                        continue
                    deps.append(dep2module(line))
        return deps
    
    def get_source_dependencies(self, source):
        module_filter = re.compile('^([\w\[\]\-]+)([\<\>\=]+)([\w\.]+)$')

        def dep2module(dep):
            res = module_filter.search(dep)
            if not res:
                if all([s not in '()!=<>' for s in dep]):
                    return InstallModule(dep)
                return None
            name = res.group(1)
            if '[' in name:
                name = name.split('[')[0]
            return InstallModule(
                res.group(1),
                [VersionRequirement(LooseVersion(res.group(3)), res.group(2))]
            )

        def parse_requirements(req_path):
            line_iter = (line.strip() for line in open(req_path))
            modules = [
                dep2module(line) for line in line_iter
                if line and not line.startswith('#') and not line.startswith('[')
            ]
            return [m for m in modules if m is not None]

        _, ext = os.path.splitext(source)
        if ext not in ('.gz', '.zip'):
            print('Error: Not supported file extension:', ext)
            return []

        requires_rel_path = None
        fname = None
        base_name = os.path.basename(source)
        req_end_path = '.egg-info/requires.txt'
        extract_dir = os.path.join(self._tmp_dir, str(uuid.uuid4()))
        os.mkdir(extract_dir)
        cd = os.getcwd()

        if ext == '.gz':
            fname = base_name.replace('.tar.gz', '')
            with tarfile.open(source, 'r:gz') as tar:
                member = tar.next()
                while member:
                    if member.name.endswith(req_end_path):
                        requires_rel_path = member.name
                        break
                    member = tar.next()
                os.chdir(extract_dir)
                tar.extractall()
                os.chdir(cd)

        if ext == '.zip':
            fname = base_name.replace('.zip', '')
            with ZipFile(source) as zip:
                for name in zip.namelist():
                    if name.endswith(req_end_path):
                        requires_rel_path = name
                        break
                os.chdir(extract_dir)
                zip.extractall()
                os.chdir(cd)
        
        if not os.listdir(extract_dir):
            print(    'Error: "%s" is not decompressed.' % (fname or base_name))
            return []
        
        self._extract_dirs[fname] = os.path.join(extract_dir, fname)

        if not requires_rel_path:
            print('    Warning: requires.txt is not found in ' + base_name)
            return []

        requires_path = os.path.join(extract_dir, requires_rel_path)
        return parse_requirements(requires_path)
   
    def get_dependencies(self, module_file):
        _, ext = os.path.splitext(module_file)
        if ext == '.whl':
            return self.get_whl_dependencies(module_file)
        return self.get_source_dependencies(module_file)

    def get_module_recursive(self, module, depth=0):
        module_files = []
        module_file = self.download_module(module)
        if not module_file:
            return module_files
        module_files = [(module_file, depth)]
        deps = self.get_dependencies(module_file)
        for dep in deps:
            if not dep:
                continue
            dep_modules = self.get_module_recursive(dep, depth-1)
            if not dep_modules:
                print('files for module "%s" are not found.' % dep.name)
                continue
            module_files += dep_modules
        if depth < 0:
            return module_files
        module_files.sort(key=lambda e:e[1])
        return self.clean_duplicated_files([e[0] for e in module_files])

    def clean_duplicated_files(self, module_files):
        result = []
        cache = set()
        for fpath in module_files:
            if fpath in cache:
                continue
            cache.add(fpath)
            result.append(fpath)
        return result

    def install_whl(self, whl_path):
        cmd = '%s install %s' % (self._pip_path, whl_path)
        subprocess.run(cmd.split())
        print('')
    
    def install_source(self, source_path):
        base_name = os.path.basename(source_path)
        fname, ext = os.path.splitext(base_name)
        if not ext in ('.gz', '.zip'):
            print('Error: Unsupported file type:', base_name)
            return
        if ext == '.gz':
            fname = base_name.replace('.tar.gz', '')
        if fname not in self._extract_dirs:
            print('Error: Failed to extract source file:', base_name)
            return
        cd = os.getcwd()
        os.chdir(self._extract_dirs[fname])
        subprocess.run('python setup.py install'.split())
        os.chdir(cd)
        print('')

    def install_module(self, module_file):
        base_name = os.path.basename(module_file)
        name, ext = os.path.splitext(module_file)
        print('Installing %s' % base_name)
        if ext == '.whl':
            self.install_whl(module_file)
            return
        self.install_source(module_file)

    def get_pip_version(self):
        result = self.get_command_result('pip --version')
        return result.split()[1]

    def check_pip_version(self):
        print('Checking pip version...')
        current_version = self.get_pip_version()
        project = self.get_project_json('pip')
        latest_version = project['info']['version']
        print('  current version:', current_version)
        print('  latest version: ', latest_version)
        return self.compare_version(current_version, latest_version, '>=')

    def upgrade_pip(self):
        print('Upgrading pip to latest version...')
        module_files = self.get_module_recursive(InstallModule('pip'))
        module_files = self.clean_duplicated_files(module_files)
        for module_file in module_files:
            self.install_module(module_file)

    def setup_skip_counter(self, modules):
        self._top_module_names = [module.name for module in modules]
        self._skipped_top_module_count = 0

    def count_skip_module(self, module_name):
        if module_name in self._top_module_names:
            self._skipped_top_module_count += 1
    
    def is_all_top_module_skipped(self):
        return self._skipped_top_module_count >= len(self._top_module_names)

    def check_all_module_files_installed(self, module_files):
        return all([
            self.is_already_installed(os.path.basename(fpath))
            for fpath in module_files
        ])

    def start(self):
        if not self._pip_path:
            print('Error: pip is not installed.')
            sys.exit(1)

        if not self._curl_path:
            print('Error: curl is not installed.')
            sys.exit(1)

        if not self.check_pip_version():
            self.upgrade_pip()

        if not Wheel:
            print('Error: pip module is not installed.')
            sys.exit(1)

        modules = self.get_modules()
        if not modules:
            print('Error: No module specified with arguments.')
            sys.exit(1)

        self.setup_skip_counter(modules)

        with tempfile.TemporaryDirectory() as tmp_dir:
            self._tmp_dir = tmp_dir
            print('Start to get module files as recursive')
            module_files = []
            for module in modules:
                print('Getting module files for %s' % module.name)
                fetched_module_files = self.get_module_recursive(module)
                if self.check_all_module_files_installed(fetched_module_files):
                    print('  Already installed:', module.name)
                    self.count_skip_module(module.name)
                    continue
                module_files += fetched_module_files

            if not module_files:
                if self.is_all_top_module_skipped():
                    print('Warning: No module has been installed.')
                else:
                    print('Error: No module files found...')
                sys.exit(1)

            module_files = self.clean_duplicated_files(module_files)

            print('Start to install module files')
            for module_file in module_files:
                fname = os.path.basename(module_file)
                if self.is_already_installed(fname):
                    print('Already installed:', fname)
                    continue
                self.install_module(module_file)
        
        print('Successfully to install all modules.')


def main():
    ModuleInstaller().start()


if __name__ == '__main__':
    main()

