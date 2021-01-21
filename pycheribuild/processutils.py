# PYTHON_ARGCOMPLETE_OK
#
# SPDX-License-Identifier: BSD-2-Clause
#
# Copyright (c) 2016-2020 Alex Richardson
#
# This work was supported by Innovate UK project 105694, "Digital Security by
# Design (DSbD) Technology Platform Prototype".
#
# This software was developed by SRI International and the University of
# Cambridge Computer Laboratory under DARPA/AFRL contract FA8750-10-C-0237
# ("CTSRD"), as part of the DARPA CRASH research programme.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

import contextlib
import fcntl
import functools
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import typing
from pathlib import Path
from subprocess import CompletedProcess

from .colour import AnsiColour, coloured
from .utils import (ConfigBase, fatal_error, get_global_config, OSInfo, status_update, Type_T, warning_message)

__all__ = ["print_command", "get_compiler_info", "CompilerInfo", "popen", "popen_handle_noexec",  # no-combine
           "run_command", "latest_system_clang_tool", "commandline_to_str", "set_env", "extract_version",  # no-combine
           "get_program_version", "check_call_handle_noexec", "get_version_output", "keep_terminal_sane",  # no-combine
           "run_and_kill_children_on_exit"]  # no-combine


def __filter_env(env: dict) -> dict:
    result = dict()
    for k, v in env.items():
        if k not in os.environ or os.environ[k] != v:
            result[k] = v
    return result


@contextlib.contextmanager
def set_env(*, print_verbose_only=True, config: ConfigBase = None, **environ):
    """
    Temporarily set the process environment variables.

    >>> with set_env(PLUGINS_DIR=u'test/plugins'):
    ...   "PLUGINS_DIR" in os.environ
    True

    >>> "PLUGINS_DIR" in os.environ
    False

    """
    if config is None:
        config = get_global_config()  # TODO: remove
    old_environ = dict(os.environ)
    # make sure all environment variables are converted to string
    str_environ = dict((str(k), str(v)) for k, v in environ.items())
    for k, v in str_environ.items():
        print_command("export", k + "=" + v, print_verbose_only=print_verbose_only, config=config)
    os.environ.update(str_environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old_environ)


class TtyState:
    # noinspection PyBroadException
    def __init__(self, fd: "typing.TextIO"):
        self.fd = fd
        try:
            self.attrs = termios.tcgetattr(fd)
        except Exception:
            # Can happen if sys.stdin/sys.stdout/sys.stderr is not a TTY
            self.attrs = None
        try:
            self.flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        except Exception:
            # Can happen if sys.stdin/sys.stdout/sys.stderr is not a real file.  When running tests with pytest, this
            # will raise UnsupportedOperation("redirected stdin is pseudofile, has no fileno()")
            self.flags = None

    def _restore_attrs(self):
        new_attrs = termios.tcgetattr(self.fd)
        if new_attrs == self.attrs:
            return
        warning_message("TTY flags for", self.fd.name, "changed, resetting them")
        print("Previous state", self.attrs)
        print("New state", new_attrs)
        try:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.attrs)
            termios.tcdrain(self.fd)
        except Exception as e:
            warning_message("Error while restoring TTY flags:", e)
        new_attrs = termios.tcgetattr(self.fd)
        if new_attrs != self.attrs:
            warning_message("Failed to restore TTY flags for", self.fd.name)
            print("Previous state", self.attrs)
            print("New state", new_attrs)

    def _restore_flags(self):
        new_flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        if new_flags == self.flags:
            return
        warning_message("FD flags for", self.fd.name, "changed, resetting them")
        print("Previous flags", self.flags)
        print("New flags", new_flags)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, self.flags)
        new_flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        if new_flags != self.flags:
            warning_message("Failed to restore TTY flags for", self.fd.name)
            print("Previous flags", self.flags)
            print("New flags", new_flags)

    def restore(self):
        if self.attrs is not None:  # Not a TTY
            self._restore_attrs()
        if self.flags is not None:  # Not a real file?
            self._restore_flags()


@contextlib.contextmanager
def suppress_sigttou(suppress=True):
    if suppress:
        hdlr = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        yield
    finally:
        if suppress:
            # noinspection PyUnboundLocalVariable
            signal.signal(signal.SIGTTOU, hdlr)


@contextlib.contextmanager
def keep_terminal_sane(gave_tty_control=False):
    # Programs such as QEMU can change the terminal state and if they don't exit cleanly this state is
    # propagated to the shell that invoked cheribuild.
    # This function attempts to restore the stdin/stdout/stderr state in those cases:
    stdin_state = TtyState(sys.stdin)
    stdout_state = TtyState(sys.stdout)
    stderr_state = TtyState(sys.stderr)
    try:
        yield
    finally:
        # Can seemingly get unwanted SIGTTOU's whilst restoring so just ignore
        # them temporarily.
        with suppress_sigttou(suppress=gave_tty_control):
            stdin_state.restore()
            stdout_state.restore()
            stderr_state.restore()


def print_command(arg1: "typing.Union[str, typing.Sequence[typing.Any]]", *remaining_args, output_file=None,
                  colour=AnsiColour.yellow, cwd=None, env=None, sep=" ", print_verbose_only=False,
                  config: ConfigBase = None, **kwargs):
    if config is None:
        config = get_global_config()  # TODO: remove
    if config.quiet or (print_verbose_only and not config.verbose):
        return
    # also allow passing a single string
    if not type(arg1) is str:
        all_args = arg1
        arg1 = all_args[0]
        remaining_args = all_args[1:]
    prefix = ("cd", shlex.quote(str(cwd)), "&&") if cwd else tuple()
    if env:
        # only print the changed environment entries
        new_env_vars = __filter_env(env)
        if new_env_vars:
            envvars = coloured(AnsiColour.cyan, commandline_to_str(k + "=" + str(v) for k, v in new_env_vars.items()))
            prefix += ("env", envvars)
    # comma in tuple is required otherwise it creates a tuple of string chars
    new_args = (shlex.quote(str(arg1)),) + tuple(map(shlex.quote, map(str, remaining_args)))
    if output_file:
        new_args += (">", str(output_file))
    # Avoid a space before the actual command if there is no prefic:
    if not prefix:
        print(coloured(colour, new_args, sep=sep), flush=True, **kwargs)
    else:
        print(coloured(colour, prefix, sep=sep), coloured(colour, new_args, sep=sep), flush=True, **kwargs)


def get_interpreter(cmdline: "typing.Sequence[str]") -> "typing.Optional[typing.List[str]]":
    """
    :param cmdline: The command to check
    :return: The interpreter command if the executable does not have execute permissions
    """
    executable = Path(cmdline[0])
    print(executable, os.access(str(executable), os.X_OK), cmdline)
    if not executable.exists():
        executable = Path(shutil.which(str(executable)))
    status_update(executable, "is not executable, looking for shebang:", end=" ")
    with executable.open("r", encoding="utf-8") as f:
        first_line = f.readline()
        if first_line.startswith("#!"):
            interpreter = shlex.split(first_line[2:])
            status_update("Will run", executable, "using", interpreter)
            return interpreter
        else:
            status_update("No shebang found.")
            return None


def _make_called_process_error(retcode, args, *, stdout=None, stderr=None, cwd=None):
    err = subprocess.CalledProcessError(retcode, args, output=stdout, stderr=stderr)
    err.cwd = cwd
    return err


def check_call_handle_noexec(cmdline: "typing.List[str]", **kwargs):
    try:
        with keep_terminal_sane():
            return subprocess.check_call(cmdline, **kwargs)
    except PermissionError as e:
        interpreter = get_interpreter(cmdline)
        if interpreter:
            with keep_terminal_sane():
                return subprocess.check_call(interpreter + cmdline, **kwargs)
        raise _make_called_process_error(e.errno, cmdline, cwd=kwargs.get("cwd", None), stderr=str(e).encode("utf-8"))
    except FileNotFoundError as e:
        raise _make_called_process_error(e.errno, cmdline, cwd=kwargs.get("cwd", None), stderr=str(e).encode("utf-8"))


def popen_handle_noexec(cmdline: "typing.List[str]", **kwargs) -> subprocess.Popen:
    try:
        return subprocess.Popen(cmdline, **kwargs)
    except PermissionError as e:
        interpreter = get_interpreter(cmdline)
        if interpreter:
            return subprocess.Popen(interpreter + cmdline, **kwargs)
        raise _make_called_process_error(e.errno, cmdline, cwd=kwargs.get("cwd", None), stderr=str(e).encode("utf-8"))
    except FileNotFoundError as e:
        raise _make_called_process_error(e.errno, cmdline, cwd=kwargs.get("cwd", None), stderr=str(e).encode("utf-8"))


# https://stackoverflow.com/a/15257702/894271
def _new_tty_foreground_process_group():
    os.setpgrp()
    with suppress_sigttou():
        tty = os.open('/dev/tty', os.O_RDWR)
        os.tcsetpgrp(tty, os.getpgrp())
        os.close(tty)


# Python 3.7 has contextlib.nullcontext
class FakePopen:
    def kill(self):
        pass

    def terminate(self):
        pass

    @staticmethod
    def poll():
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def popen(cmdline, print_verbose_only=False, run_in_pretend_mode=False, *, config: ConfigBase = None,
          **kwargs) -> subprocess.Popen:
    if config is None:
        config = get_global_config()  # TODO: remove
    print_command(cmdline, cwd=kwargs.get("cwd"), env=kwargs.get("env"), config=config,
                  print_verbose_only=print_verbose_only)
    if not run_in_pretend_mode and config.pretend:
        # noinspection PyTypeChecker
        return FakePopen()
    return popen_handle_noexec(cmdline, **kwargs)


# noinspection PyShadowingBuiltins
def run_command(*args, capture_output=False, capture_error=False, input: "typing.Union[str, bytes]" = None,
                timeout=None, print_verbose_only=False, run_in_pretend_mode=False, raise_in_pretend_mode=False,
                no_print=False, replace_env=False, give_tty_control=False, expected_exit_code=0,
                allow_unexpected_returncode=False, config: ConfigBase = None, **kwargs):
    if config is None:
        config = get_global_config()  # TODO: remove
    if len(args) == 1 and isinstance(args[0], (list, tuple)):
        cmdline = args[0]  # list with parameters was passed
    else:
        cmdline = args
    assert "_ARGCOMPLETE" not in os.environ, "Should not execute any programs as part of bash completion!"
    cmdline = list(map(str, cmdline))  # ensure it's all strings so that subprocess can handle it
    # When running scripts from a noexec filesystem try to read the interpreter and run that
    if not no_print:
        print_command(cmdline, cwd=kwargs.get("cwd"), env=kwargs.get("env"), print_verbose_only=print_verbose_only,
                      config=config)
    if "cwd" in kwargs:
        kwargs["cwd"] = str(kwargs["cwd"])
    else:
        # os.getcwd() raises an exception if the cwd was deleted
        try:
            kwargs["cwd"] = os.getcwd()
        except FileNotFoundError:
            kwargs["cwd"] = tempfile.gettempdir()
    if not run_in_pretend_mode and config.pretend:
        return CompletedProcess(args=cmdline, returncode=0, stdout=b"", stderr=b"")
    # actually run the process now:
    if input is not None:
        assert "stdin" not in kwargs  # we need to use stdin here
        kwargs['stdin'] = subprocess.PIPE
        if not isinstance(input, bytes):
            input = str(input).encode("utf-8")
    if capture_output:
        assert "stdout" not in kwargs  # we need to use stdout here
        kwargs["stdout"] = subprocess.PIPE
    if capture_error:
        assert "stderr" not in kwargs  # we need to use stdout here
        kwargs["stderr"] = subprocess.PIPE
    elif config.quiet and "stdout" not in kwargs:
        kwargs["stdout"] = subprocess.DEVNULL

    if "env" in kwargs:
        env_arg = kwargs["env"]  # type: typing.Dict[str, str]
        if not replace_env:
            new_env = os.environ.copy()
            env = {k: str(v) for k, v in env_arg.items()}  # make sure everything is a string
            new_env.update(env)
            kwargs["env"] = new_env
        else:
            kwargs["env"] = dict((k, str(v)) for k, v in env_arg.items())
    if give_tty_control:
        kwargs["preexec_fn"] = _new_tty_foreground_process_group
    stdout = b""
    stderr = b""
    # Some programs (such as QEMU) can mess up the TTY state if they don't exit cleanly
    with keep_terminal_sane(give_tty_control):
        with popen_handle_noexec(cmdline, **kwargs) as process:
            try:
                stdout, stderr = process.communicate(input, timeout=timeout)
            except KeyboardInterrupt:
                process.send_signal(signal.SIGINT)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                assert timeout is not None
                raise subprocess.TimeoutExpired(process.args, timeout, output=stdout, stderr=stderr)
            except BrokenPipeError:
                # just return the exit code
                process.kill()
                retcode = process.wait()
                raise _make_called_process_error(retcode, process.args, stdout=b"", cwd=kwargs["cwd"])
            except Exception:
                process.kill()
                process.wait()
                raise
            retcode = process.poll()
            if retcode != expected_exit_code and not allow_unexpected_returncode:
                if config.pretend and not raise_in_pretend_mode:
                    cwd = (". Working directory was ", kwargs["cwd"]) if "cwd" in kwargs else ()
                    fatal_error("Command ", "`" + commandline_to_str(process.args) +
                                "` failed with unexpected exit code ", retcode, *cwd, sep="", pretend=config.pretend)
                else:
                    raise _make_called_process_error(retcode, process.args, stdout=stdout, stderr=stderr,
                                                     cwd=kwargs["cwd"])
            return CompletedProcess(process.args, retcode, stdout, stderr)


def commandline_to_str(args: "typing.Iterable[typing.Union[str,Path]]") -> str:
    return " ".join((shlex.quote(str(s)) for s in args))


class CompilerInfo(object):
    def __init__(self, path: Path, compiler: str, version: "typing.Tuple[int]", version_str: str, default_target: str,
                 *, config: ConfigBase):
        self.path = path
        self.compiler = compiler
        self.version = version
        self.version_str = version_str
        self.default_target = default_target
        self.config = config
        self._resource_dir = None  # type: typing.Optional[Path]
        self._supported_warning_flags = dict()  # type: typing.Dict[str, bool]
        assert compiler in ("unknown compiler", "clang", "apple-clang", "gcc"), "unknown type: " + compiler

    def get_resource_dir(self) -> Path:
        # assert self.is_clang, self.compiler
        if not self._resource_dir:
            if not self.path.exists():
                return Path("/unknown/resource/dir")  # avoid failing in jenkins
            # Clang 5.0 added the -print-resource-dir flag
            if self.is_clang and self.version >= (5, 0):
                resource_dir = run_command(self.path, "-print-resource-dir", config=self.config,
                                           print_verbose_only=True, capture_output=True,
                                           run_in_pretend_mode=True).stdout.decode("utf-8").strip()
                assert resource_dir, "-print-resource-dir no longer works?"
                self._resource_dir = Path(resource_dir)
            else:
                # pretend to compile an existing source file and capture the -resource-dir output
                cc1_cmd = run_command(self.path, "-###", "-xc", "-c", "/dev/null", config=self.config,
                                      capture_error=True, print_verbose_only=True, run_in_pretend_mode=True)
                resource_dir_pat = re.compile(b'"-cc1".+"-resource-dir" "([^"]+)"')
                self._resource_dir = Path(resource_dir_pat.search(cc1_cmd.stderr).group(1).decode("utf-8"))
        return self._resource_dir

    def _supports_warning_flag(self, flag: str):
        assert flag.startswith("-W")
        try:
            result = run_command(self.path, flag, "-fsyntax-only", "-xc", "/dev/null", "-Werror=unknown-warning-option",
                                 print_verbose_only=True, run_in_pretend_mode=True, capture_error=True,
                                 allow_unexpected_returncode=True, config=self.config)
        except (subprocess.CalledProcessError, OSError) as e:
            warning_message("Failed to check for", flag, "support:", e)
            return False
        return result.returncode == 0

    def supports_warning_flag(self, flag: str):
        result = self._supported_warning_flags.get(flag)
        if result is None:
            result = self._supports_warning_flag(flag)
            self._supported_warning_flags[flag] = result
        return result

    def get_matching_binutil(self, binutil):
        assert self.is_clang
        name = self.path.name
        version_suffix = ""
        for basename in ("clang++", "clang-cpp", "clang"):
            if name.startswith(basename):
                version_suffix = name[len(basename):]
        # Try to find a binutil with the same version suffix first
        real_compiler_path = self.path.resolve() if self.path.exists() else self.path
        result = real_compiler_path.parent / (binutil + version_suffix)
        if result.exists():
            return result
        else:
            status_update("Could not find version-suffixed", binutil, "in expected path", result)
        if real_compiler_path != self.path.parent:
            # Clang is installed in a different directory (e.g. /usr/lib/llvm-7) -> should be unversioned
            result = real_compiler_path.parent / binutil
            if not result.exists():
                warning_message("Could not find", binutil, "in expected path", result)
                result = None
        if not result:
            result = shutil.which(binutil)  # fall back to the default and assume clang can find the right one
        return result

    @property
    def is_clang(self):
        return self.compiler in ("clang", "apple-clang")

    @property
    def is_apple_clang(self):
        return self.compiler == "apple-clang"

    def __repr__(self):
        return "{} ({} {})".format(self.path, self.compiler, ".".join(map(str, self.version)))


_cached_compiler_infos = dict()  # type: typing.Dict[Path, CompilerInfo]


def get_compiler_info(compiler: "typing.Union[str, Path]", *, config: ConfigBase) -> CompilerInfo:
    assert compiler is not None
    compiler = Path(compiler)
    if not compiler.is_absolute():
        found_in_path = shutil.which(str(compiler))
        assert found_in_path is not None, "Called with non-existent compiler " + str(compiler)
        compiler = Path(found_in_path)

    if compiler not in _cached_compiler_infos:
        # Avoid querying the same compiler twice if it is a symlink
        compiler_realpath = compiler.resolve() if compiler.exists() else compiler
        if compiler_realpath in _cached_compiler_infos:
            _cached_compiler_infos[compiler] = _cached_compiler_infos[compiler_realpath]
        compiler = compiler_realpath
    if compiler not in _cached_compiler_infos:
        clang_version_pattern = re.compile(b"clang version (\\d+)\\.(\\d+)\\.?(\\d+)?")
        gcc_version_pattern = re.compile(b"gcc version (\\d+)\\.(\\d+)\\.?(\\d+)?")
        apple_llvm_version_pattern = re.compile(b"Apple (?:clang|LLVM) version (\\d+)\\.(\\d+)\\.?(\\d+)?")
        # TODO: could also use -dumpmachine to get the triple
        target_pattern = re.compile(b"Target: (.+)")
        executed_sucessfully = True
        # clang prints this output to stderr
        try:
            # Use -v instead of --version to support both gcc and clang
            # Note: for clang-cpp/cpp we need to have stdin as devnull
            version_cmd = run_command(compiler, "-v", capture_error=True, print_verbose_only=True,
                                      run_in_pretend_mode=True, config=config,
                                      stdin=subprocess.DEVNULL, capture_output=True)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr if e.stderr else b"FAILED: " + str(e).encode("utf-8")
            version_cmd = CompletedProcess(e.cmd, e.returncode, e.output, stderr)
            executed_sucessfully = False
        except OSError as e:
            version_cmd = CompletedProcess([compiler, "-v"], e.errno, b"", str(e).encode("utf-8"))
            executed_sucessfully = False

        clang_version = clang_version_pattern.search(version_cmd.stderr)
        apple_llvm_version = apple_llvm_version_pattern.search(version_cmd.stderr)
        gcc_version = gcc_version_pattern.search(version_cmd.stderr)
        target = target_pattern.search(version_cmd.stderr)
        kind = "unknown compiler"
        version = (0, 0, 0)
        version_str = "unknown version"
        target_string = target.group(1).decode("utf-8") if target else ""
        if gcc_version:
            kind = "gcc"
            version = tuple(map(int, gcc_version.groups()))
            version_str = gcc_version.group(0).decode("utf-8")
        elif apple_llvm_version:
            kind = "apple-clang"
            version = tuple(map(int, apple_llvm_version.groups()))
            version_str = apple_llvm_version.group(0).decode("utf-8")
        elif clang_version:
            kind = "clang"
            version = tuple(map(int, clang_version.groups()))
            version_str = clang_version.group(0).decode("utf-8")
        else:
            warning_message("Could not detect compiler info for", compiler, "- output was", version_cmd.stderr)
        if config.verbose:
            print(compiler, "is", kind, "version", version, "with default target", target_string)
        result = CompilerInfo(compiler, kind, version, version_str, target_string, config=config)
        # Don't cache the result if the -v command failed (e.g. compiler doesn't exist yet)
        if executed_sucessfully:
            _cached_compiler_infos[compiler] = result
        return result
    return _cached_compiler_infos[compiler]


# Cache the versions
@functools.lru_cache(maxsize=20)
def get_version_output(program: Path, command_args: tuple = None, *, config: ConfigBase = None) -> "bytes":
    if config is None:
        config = get_global_config()  # TODO: remove
    if command_args is None:
        command_args = ["--version"]
    prog = run_command([str(program)] + list(command_args), config=config, stdin=subprocess.DEVNULL,
                       stderr=subprocess.STDOUT, capture_output=True, run_in_pretend_mode=True)
    return prog.stdout


@functools.lru_cache(maxsize=20)
def get_program_version(program: Path, command_args: tuple = None, component_kind: "typing.Type[Type_T]" = int,
                        regex=None, program_name: bytes = None, *,
                        config: ConfigBase = None) -> "typing.Tuple[Type_T, Type_T, Type_T]":
    if config is None:
        config = get_global_config()  # TODO: remove
    if program_name is None:
        program_name = program.name.encode("utf-8")
    stdout = get_version_output(program, command_args=command_args, config=config)
    return extract_version(stdout, component_kind, regex, program_name)


# extract the version component from program output such as "git version 2.7.4"
def extract_version(output: bytes, component_kind: "typing.Type[Type_T]" = int, regex: "typing.Pattern" = None,
                    program_name: bytes = b"") -> "typing.Tuple[Type_T, Type_T, Type_T]":
    if regex is None:
        prefix = program_name + b" " if program_name else b""
        regex = re.compile(prefix + b"version\\s+(\\d+)\\.(\\d+)\\.?(\\d+)?")
    elif isinstance(regex, bytes):
        regex = re.compile(regex)
    match = regex.search(output)
    if not match:
        print(output)
        raise ValueError("Expected to match regex " + str(regex))
    # noinspection PyTypeChecker
    return tuple(map(component_kind, match.groups()))


def latest_system_clang_tool(config: ConfigBase, basename: str,
                             fallback_basename: "typing.Optional[str]") -> typing.Optional[Path]:
    if "_ARGCOMPLETE" in os.environ:  # Avoid expensive lookup when tab-completing
        return None if fallback_basename is None else Path(fallback_basename)

    # Only search in /usr/bin/ and /usr/local/bin by default.
    # If users want to use other versions they should explicitly pass --cc-path, etc
    search_path = [Path("/usr/local/bin"), Path("/usr/bin")]
    valid_regex = re.compile(re.escape(basename) + r"[-\d.]*$")
    results = []
    for search_dir in search_path:
        if not search_dir.exists():
            continue
        # Note: os.listdir is faster than path.glob("*") since we don't have to stat all files
        for candidate_name in os.listdir(str(search_dir)):
            if not candidate_name.startswith(basename) or not valid_regex.match(candidate_name):
                continue
            # print("Checking compiler candidate", candidate)
            candidate = search_dir / candidate_name
            info = get_compiler_info(candidate, config=config)  # Global config not initialized yet
            if OSInfo.IS_MAC and not info.is_apple_clang:
                # print("Ignoring", candidate, "since it is not apple clang and won't be able to build host binaries")
                continue
            # Minimum version is 4.0
            if info.version < (4, 0, 0) and not info.is_apple_clang:
                # print("Ignoring", basename, "candidate", candidate, "since it is too old:", info.version)
                continue
            results.append((candidate, info.is_apple_clang, info.version))
    if not results:
        if fallback_basename is None:
            return None
        fullpath = shutil.which(fallback_basename)
        return Path(fullpath) if fullpath else Path("/could/not/find", fallback_basename)
    # Find the newest version (and prefer apple-clang to non-apple clang
    # since it is required on macOS to build any binary
    # print("Candidates for", basename, results)
    newest = max(results, key=lambda p: (p[1], p[2]))
    return newest[0]


def run_and_kill_children_on_exit(fn: "typing.Callable[[], typing.Any]"):
    error = False
    try:
        opgrp = os.getpgrp()
        if opgrp != os.getpid():
            # Create new process group and become its leader
            os.setpgrp()
            # Preserve whether our process group is the terminal leader
            with suppress_sigttou():
                tty = os.open('/dev/tty', os.O_RDWR)
                if os.tcgetpgrp(tty) == opgrp:
                    os.tcsetpgrp(tty, os.getpgrp())
        fn()
    except KeyboardInterrupt:
        error = True
        sys.exit("Exiting due to Ctrl+C")
    except subprocess.CalledProcessError as err:
        error = True
        extra_msg = (". Working directory was ", err.cwd) if hasattr(err, "cwd") else ()
        if err.stderr is not None:
            extra_msg += ("\nStandard error was:\n", err.stderr.decode("utf-8"))
        fatal_error("Command ", "`" + commandline_to_str(err.cmd) + "` failed with non-zero exit code ",
                    err.returncode, *extra_msg, fatal_when_pretending=True, sep="", exit_code=err.returncode)
    finally:
        if error:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.killpg(0, signal.SIGTERM)  # Tell all child processes to exit
