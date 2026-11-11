from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import math
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WIN = os.name == 'nt'


class LaunchError(Exception):
    pass


class InstanceLock:
    def __enter__(self):
        if IS_WIN:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.restype = ctypes.c_void_p
            name = 'Local\\Quinn-' + hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:24]
            self.handle = kernel.CreateMutexW(None, False, name)
            if not self.handle or ctypes.get_last_error() == 183:
                if self.handle:
                    kernel.CloseHandle(ctypes.c_void_p(self.handle))
                raise LaunchError('Quinn launcher is already open.')
        return self

    def __exit__(self, *_):
        if IS_WIN:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))


class Component:
    def __init__(self, name, log, window=False):
        self.name, self.log, self.window = name, log, window
        self.proc = None
        self.job = None
        self.state = 'stopped'

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, command, cwd=ROOT, env=None):
        if self.alive:
            return
        self.state = 'starting'
        options = dict(cwd=str(cwd), env=env)
        if not self.window:
            options.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        if IS_WIN:
            import win32job
            options['creationflags'] = 4 | (subprocess.CREATE_NEW_CONSOLE if self.window else subprocess.CREATE_NO_WINDOW)
            self.job = win32job.CreateJobObject(None, f'Quinn-{os.getpid()}-{self.name}-{time.monotonic_ns()}')
            limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            limits['BasicLimitInformation']['LimitFlags'] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
        else:
            options['start_new_session'] = True
        try:
            self.proc = subprocess.Popen(command, **options)
            if IS_WIN:
                win32job.AssignProcessToJobObject(self.job, self.proc._handle)
                status = ctypes.windll.ntdll.NtResumeProcess(ctypes.c_void_p(int(self.proc._handle)))
                if status:
                    raise OSError(f'Could not resume process: {status}')
            if not self.window:
                threading.Thread(target=self.read, args=(self.proc,), daemon=True).start()
        except Exception:
            self.stop()
            self.state = 'failed'
            raise

    def read(self, proc):
        try:
            for line in iter(proc.stdout.readline, b''):
                text = line.decode('utf-8', errors='replace').strip()
                if text and not (self.name == 'bridge' and any(noise in text for noise in ('/v1/next', 'OPTIONS', '/v1/launcher', '/v1/status', 'CORS preflight'))):
                    self.log(self.name, text)
        finally:
            proc.stdout.close()

    def stop(self):
        self.state = 'stopping'
        if self.job:
            import win32job
            win32job.TerminateJobObject(self.job, 0)
            self.job.Close()
            self.job = None
        elif self.alive:
            if not IS_WIN:
                os.killpg(self.proc.pid, signal.SIGTERM)
            else:
                self.proc.kill()
        if self.proc:
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        self.state = 'stopped'


class Launcher:
    def __init__(self):
        self.logs = deque(maxlen=16)
        self.log_lock = threading.Lock()
        self.components = {name: Component(name, self.log, name == 'sniper') for name in ('kobold', 'bridge', 'tavern', 'discord', 'sniper')}
        self.commands = queue.Queue()
        self.cancel = threading.Event()
        self.force = threading.Event()
        self.quit = False
        self.worker = None
        self.operation = ''
        self.session = None
        self.token = ''
        try:
            config = json.loads((ROOT / 'quinn_config.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            config = {}
        self.dry = bool(config.get('dry_run', False)) if isinstance(config, dict) else False
        self.settings = {}
        self.cfg = {}
        self.last_heartbeat = 0
        self.console_handler = None

    def log(self, name, text):
        with self.log_lock:
            self.logs.append(f'{time.strftime("%H:%M:%S")} [{name}] {text}')

    def configure(self, full):
        path = ROOT / 'quinn_config.json'
        if not path.exists():
            raise LaunchError('Create quinn_config.json from the template first.')
        self.cfg = json.loads(path.read_text(encoding='utf-8'))
        self.settings = self.cfg.get('launcher', {})
        for name, default in (('kobold', 180), ('bridge', 10), ('tavern', 60), ('browser', 30), ('discord', 30), ('shutdown', 120)):
            timeout = float(self.settings.get(name + '_timeout', default))
            if not math.isfinite(timeout) or timeout <= 0:
                raise LaunchError(f'{name}_timeout must be a finite positive number.')
        self.bridge_url = self.cfg.get('bridge_url', 'http://127.0.0.1:8765').rstrip('/')
        self.tavern_url = self.settings.get('sillytavern_url', 'http://localhost:8000').rstrip('/')
        self.kobold_url = self.settings.get('kobold_url', 'http://127.0.0.1:5001').rstrip('/')
        self.tavern_dir = self.path(self.settings.get('sillytavern_directory', str(Path(self.cfg.get('sillytavern_start', 'SillyTavern/Start.bat')).parent)))
        for module in ('aiohttp', 'discord', 'colorama') + (('win32job',) if IS_WIN else ()):
            if importlib.util.find_spec(module) is None:
                raise LaunchError(f'Missing Python dependency: {module}. Install requirements.txt with this Python.')
        self.node = shutil.which(self.settings.get('node', 'node'))
        if not self.node or not (self.tavern_dir / 'server.js').is_file() or not (self.tavern_dir / 'node_modules/express').is_dir():
            raise LaunchError('Node or SillyTavern dependencies are missing. Run SillyTavern Start.bat once to install dependencies.')
        self.firefox = self.settings.get('firefox_path') or shutil.which('firefox')
        if not self.firefox:
            for base in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
                candidate = Path(os.environ.get(base, '')) / 'Mozilla Firefox/firefox.exe'
                if candidate.is_file():
                    self.firefox = str(candidate)
                    break
        if not self.firefox or not Path(self.firefox).is_file():
            raise LaunchError('Firefox was not found. Set launcher.firefox_path.')
        for url in (self.bridge_url, self.tavern_url, self.kobold_url):
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1'):
                raise LaunchError('Launcher service URLs must use local HTTP addresses.')
        if full:
            from quinn_discord import _resolve_discord_token
            try:
                _resolve_discord_token(self.cfg)
            except SystemExit as exc:
                raise LaunchError(str(exc)) from exc
            self.kobold_exe = self.path(self.settings.get('kobold_executable', 'koboldcpp.exe'))
            self.kobold_config = self.path(self.settings.get('kobold_config', 'config.kcpps'))
            if not self.kobold_exe.is_file() or not self.kobold_config.is_file():
                raise LaunchError('KoboldCpp executable or saved configuration was not found.')
            model_cfg = json.loads(self.kobold_config.read_text(encoding='utf-8'))
            model = model_cfg.get('model_param') or model_cfg.get('model')
            if isinstance(model, list):
                model = model[0] if model else ''
            if not model or not self.path(model).is_file():
                raise LaunchError('The model in the saved KoboldCpp configuration was not found.')
            if int(model_cfg.get('port_param') or model_cfg.get('port', 5001)) != (urllib.parse.urlparse(self.kobold_url).port or 80):
                raise LaunchError('KoboldCpp config port and launcher.kobold_url disagree.')

    def path(self, value):
        path = Path(value)
        return path if path.is_absolute() else ROOT / path

    def sync_extension(self):
        source = ROOT / 'put in tavern extensions/quinn-bridge'
        target = self.tavern_dir / 'data/default-user/extensions/quinn-bridge'
        changed = [name for name in ('index.js', 'style.css', 'manifest.json') if not (target / name).exists() or (target / name).read_bytes() != (source / name).read_bytes()]
        if not changed:
            return
        if self.components['tavern'].alive:
            raise LaunchError('Stop SillyTavern before updating its Quinn extension.')
        backup = ROOT / 'backups' / ('launcher-extension-' + time.strftime('%Y%m%d-%H%M%S'))
        if target.exists():
            shutil.copytree(target, backup)
        target.mkdir(parents=True, exist_ok=True)
        for name in changed:
            shutil.copy2(source / name, target / name)
        self.log('launcher', 'Installed the Quinn extension update with a backup of the previous copy.')

    def request(self, path, data=None):
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'}
        body = None if data is None else json.dumps(data).encode()
        with urllib.request.urlopen(urllib.request.Request(self.bridge_url + path, data=body, headers=headers), timeout=2) as response:
            return json.load(response)

    def control(self, operation, **data):
        return self.request('/v1/launcher', dict(operation=operation, session=self.session, **data))

    def status(self):
        return self.request('/v1/launcher?session=' + self.session)

    def deadline(self, name, default):
        value = float(self.settings.get(name + '_timeout', default))
        if value <= 0:
            raise LaunchError(f'{name}_timeout must be positive.')
        return time.monotonic() + value

    def check_cancel(self):
        if self.cancel.is_set():
            raise LaunchError('Startup canceled.')

    def wait(self, name, timeout, predicate):
        self.stage = name
        deadline = self.deadline(name, timeout)
        while time.monotonic() < deadline:
            self.check_cancel()
            comp = self.components.get(name)
            if comp and not comp.alive:
                raise LaunchError(f'{name} exited during startup. Check its log.')
            try:
                if predicate():
                    if comp:
                        comp.state = 'ready'
                    return
            except (OSError, ValueError):
                pass
            self.cancel.wait(0.25)
        detail = ' Check the saved bridge token, character, extension, and Firefox tab.' if name == 'browser' else ''
        raise LaunchError(f'{name} readiness timed out.{detail}')

    def http(self, url, model=False):
        with urllib.request.urlopen(url, timeout=1) as response:
            if model:
                data = json.load(response)
                return bool(data.get('result')) and data['result'] not in ('ReadOnly', 'None', 'inactive')
            return response.status == 200

    def check_ports(self, full):
        for name, url in [('bridge', self.bridge_url), ('tavern', self.tavern_url)] + ([('kobold', self.kobold_url)] if full else []):
            if self.components[name].alive:
                continue
            parsed = urllib.parse.urlparse(url)
            with socket.socket() as connection:
                connection.settimeout(0.3)
                if connection.connect_ex((parsed.hostname, parsed.port or 80)) == 0:
                    raise LaunchError(f'{name} port {parsed.port or 80} is already occupied. Stop the independently launched service first.')

    def startup(self, full=True, sniper=False):
        self.stage = None
        before = {name for name, comp in self.components.items() if comp.alive}
        created_session = False
        try:
            self.configure(full)
            self.check_ports(full)
            self.sync_extension()
            self.check_cancel()
            if full:
                self.components['kobold'].start([str(self.kobold_exe), '--config', str(self.kobold_config), '--skiplauncher'])
                self.wait('kobold', 180, lambda: self.http(self.kobold_url + '/api/v1/model', True))
            env = dict(os.environ, QUINN_BRIDGE_PORT=str(urllib.parse.urlparse(self.bridge_url).port or 80))
            self.components['bridge'].start([sys.executable, '-u', str(ROOT / 'QuinnBridge/bridge/server.py')], env=env)
            self.wait('bridge', 10, lambda: self.http(self.bridge_url + '/health'))
            self.token = (ROOT / 'QuinnBridge/bridge/bridge_token.txt').read_text(encoding='utf-8').strip()
            if self.cfg.get('bridge_token') and self.cfg['bridge_token'] != self.token:
                raise LaunchError('Configured bridge_token disagrees with bridge_token.txt. Correct it or leave it blank.')
            if self.session:
                if full and not self.status()['autostart']:
                    raise LaunchError('Stop standalone SillyTavern before selecting Quinn setup.')
            else:
                self.session = self.control('create', autostart=full)['session']
                created_session = True
            self.components['tavern'].start([self.node, 'server.js', '--browserLaunchEnabled=false', '--port', str(urllib.parse.urlparse(self.tavern_url).port or 80)], cwd=self.tavern_dir, env=dict(os.environ, NODE_ENV='production'))
            self.wait('tavern', 60, lambda: self.http(self.tavern_url + '/'))
            if created_session:
                subprocess.Popen([self.firefox, '-new-tab', self.tavern_url + '/#quinn-launch=' + self.session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
            def browser_ready():
                state = self.status()
                if state['browser'] in ('failed', 'closed', 'blocked'):
                    raise LaunchError(state['error'] or 'SillyTavern extension failed.')
                return state['browser'] in ('ready', 'busy')
            if full:
                self.wait('browser', 30, browser_ready)
                env = dict(os.environ, QUINN_LAUNCH_SESSION=self.session)
                args = ['--dry-run' if self.dry else '--live']
                self.components['discord'].start([sys.executable, '-u', str(ROOT / 'quinn_discord.py'), *args], env=env)
                self.wait('discord', 30, lambda: self.status()['discord'] in ('ready', 'busy'))
            if sniper:
                self.start_sniper()
            self.log('launcher', 'Quinn is ready.' if full else 'SillyTavern is ready.')
        except Exception as exc:
            self.log('error', str(exc))
            if created_session:
                self.close_tab(0 if self.force.is_set() else 3)
                self.session = None
            for name in reversed(tuple(self.components)):
                if name not in before:
                    self.components[name].stop()
            if self.stage in self.components and self.stage not in before and not self.cancel.is_set():
                self.components[self.stage].state = 'failed'
            self.log('launcher', 'Startup rolled back; check the log before retrying.')
        finally:
            self.operation = ''

    def start_sniper(self):
        try:
            if not (ROOT / 'config.json').is_file():
                raise LaunchError('Copy config template.json to config.json and configure Sniper first.')
            self.components['sniper'].start([sys.executable, '-u', str(ROOT / 'main.py')])
            self.components['sniper'].state = 'ready'
        except Exception as exc:
            self.log('error', str(exc))

    def close_tab(self, budget=3):
        if not self.session:
            return
        try:
            self.control('close')
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                state = self.status()
                if state['browser'] == 'closed':
                    self.log('launcher', 'Managed SillyTavern tab closed.')
                    return
                if state['browser'] == 'left':
                    self.log('launcher', 'SillyTavern left the managed page; tab closure is unconfirmed.')
                    return
                if state['browser'] == 'blocked':
                    self.log('launcher', 'Firefox blocked tab closure; close the SillyTavern tab manually.')
                    return
                time.sleep(0.1)
            self.log('launcher', 'Tab close requested; closure cannot be confirmed by the browser.')
        except Exception:
            self.log('launcher', 'Tab closure unconfirmed; the managed tab will attempt cleanup when its lease expires.')

    def shutdown(self):
        try:
            if self.session and self.components['bridge'].alive:
                try:
                    self.control('drain')
                    deadline = self.deadline('shutdown', 120)
                    while not self.force.is_set() and time.monotonic() < deadline:
                        state = self.status()
                        if not state['active'] and state['browser'] != 'busy' and state['discord'] != 'busy':
                            break
                        self.force.wait(0.2)
                except Exception as exc:
                    self.log('launcher', f'Graceful stop unavailable: {exc}')
                try:
                    self.control('disconnect')
                    deadline = time.monotonic() + 3
                    while self.components['discord'].alive and not self.force.is_set() and time.monotonic() < deadline:
                        time.sleep(0.1)
                except Exception:
                    pass
                self.components['discord'].stop()
                self.close_tab(0 if self.force.is_set() else 3)
            for name in ('discord', 'tavern', 'kobold', 'bridge', 'sniper'):
                try:
                    self.components[name].stop()
                except Exception as exc:
                    self.log('error', f'Could not stop {name}: {exc}')
            self.session = None
            self.log('launcher', 'All managed components stopped.')
        finally:
            self.operation = ''

    def run_task(self, operation, target):
        if self.worker and self.worker.is_alive():
            self.log('launcher', 'An operation is already in progress.')
            return
        self.cancel.clear()
        self.force.clear()
        self.operation = operation
        def work():
            try:
                target()
            except Exception as exc:
                self.log('error', str(exc))
            finally:
                self.operation = ''
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def stop_requested(self, quit=False):
        self.quit |= quit
        if self.operation == 'stopping':
            self.force.set()
            return
        self.cancel.set()
        if self.worker and self.worker.is_alive():
            return
        self.run_task('stopping', self.shutdown)

    def input_loop(self):
        while True:
            try:
                self.commands.put(input().strip().lower())
            except EOFError:
                self.commands.put('q')
                return

    def console_close(self):
        self.cancel.set()
        self.force.set()
        try:
            if self.session:
                self.control('close')
        except Exception:
            pass
        for comp in self.components.values():
            try:
                comp.stop()
            except Exception:
                pass

    def draw(self):
        os.system('cls' if IS_WIN else 'clear')
        print('PROJECT QUINN | Mission Control\n')
        for name, comp in self.components.items():
            state = comp.state
            if comp.proc and not comp.alive and state not in ('stopped', 'stopping'):
                state = 'failed'
            print(f'  {name:<12} {state}')
        print(f'\n  Mode: {"DRY-RUN" if self.dry else "LIVE"}   {self.operation}')
        print('\n  [1] Sniper  [2] Quinn setup  [3] Everything  [t] SillyTavern')
        print('  [s] Stop / force stop  [q] Stop and quit  [d] Toggle dry-run  [r] Refresh\n')
        with self.log_lock:
            for line in self.logs:
                print('  ' + line)
        print('\n> ' + getattr(self, 'input_buffer', ''), end='', flush=True)

    def run(self):
        keyboard = IS_WIN and sys.stdin.isatty()
        self.input_buffer = ''
        if not keyboard:
            threading.Thread(target=self.input_loop, daemon=True).start()
        signal.signal(signal.SIGINT, lambda *_: self.commands.put('q'))
        if IS_WIN:
            callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
            def handler(event):
                if event in (2, 5, 6):
                    self.console_close()
                    return True
                return False
            self.console_handler = callback(handler)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(self.console_handler, True)
        redraw = 0
        stopping_pending = False
        try:
            while True:
                if keyboard:
                    import msvcrt
                    while msvcrt.kbhit():
                        character = msvcrt.getwch()
                        if character in ('\x00', '\xe0'):
                            msvcrt.getwch()
                        elif character == '\r':
                            self.commands.put(self.input_buffer.strip().lower())
                            self.input_buffer = ''
                        elif character == '\x03':
                            self.commands.put('q')
                        elif character == '\b':
                            self.input_buffer = self.input_buffer[:-1]
                        elif character.isprintable():
                            self.input_buffer += character
                        redraw = 0
                if self.session and time.monotonic() - self.last_heartbeat >= 2:
                    try:
                        self.control('heartbeat')
                    except Exception:
                        pass
                    self.last_heartbeat = time.monotonic()
                if stopping_pending and not (self.worker and self.worker.is_alive()):
                    stopping_pending = False
                    self.run_task('stopping', self.shutdown)
                if self.quit and not stopping_pending and not (self.worker and self.worker.is_alive()):
                    break
                if time.monotonic() >= redraw:
                    self.draw()
                    redraw = time.monotonic() + 1
                try:
                    choice = self.commands.get(timeout=0.1)
                except queue.Empty:
                    continue
                if choice in ('s', 'q'):
                    stopping_pending = bool(self.worker and self.worker.is_alive() and self.operation != 'stopping')
                    self.stop_requested(choice == 'q')
                elif choice == 'd' and not self.operation:
                    self.dry = not self.dry
                elif choice in ('1', '2', '3', 't'):
                    action = self.start_sniper if choice == '1' else lambda: self.startup(choice != 't', choice == '3')
                    self.run_task('starting', action)
                redraw = 0
        finally:
            self.console_close()


def main():
    if sys.version_info < (3, 10):
        print('Quinn requires Python 3.10 or newer.')
        return 1
    try:
        with InstanceLock():
            Launcher().run()
    except (LaunchError, ImportError) as exc:
        print(f'Quinn: {exc}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
