#!/usr/bin/python3
# Copyright 2013 The Emscripten Authors.  All rights reserved.
# Emscripten is available under two separate licenses, the MIT license and the
# University of Illinois/NCSA Open Source License.  Both these licenses can be
# found in the LICENSE file.

"""Runs csmith, a C fuzzer, and looks for bugs.

CSMITH_PATH should be set to something like /usr/local/include/csmith
"""

import os
import sys
import shutil
import random
import subprocess
from subprocess import check_call, PIPE, STDOUT, CalledProcessError
from distutils.spawn import find_executable

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(script_dir))))

from tools import shared
from tools import config

# can add flags like --no-threads --ion-offthread-compile=off
engine = eval('config.' + sys.argv[1]) if len(sys.argv) > 1 else config.JS_ENGINES[0]

print('testing js engine', engine)

TEST_BINARYEN = 1

CSMITH = os.environ.get('CSMITH', find_executable('csmith'))
assert CSMITH, 'Could not find CSmith on your PATH. Please set the environment variable CSMITH.'
CSMITH_PATH = os.environ.get('CSMITH_PATH', '/usr/include/csmith')
assert os.path.exists(CSMITH_PATH), 'Please set the environment variable CSMITH_PATH.'
CSMITH_CFLAGS = ['-I', CSMITH_PATH]

filename = os.path.join(os.getcwd(), 'temp_fuzzcode' + str(os.getpid()) + '_')

shared.DEFAULT_TIMEOUT = 5

tried = 0

notes = {'invalid': 0, 'embug': 0}

fails = 0

while 1:
  if random.random() < 0.666:
    opts = '-O' + str(random.randint(0, 3))
  else:
    if random.random() < 0.5:
      opts = '-Os'
    else:
      opts = '-Oz'
  print('opt level:', opts)

  print('Tried %d, notes: %s' % (tried, notes))
  print('1) Generate source')
  extra_args = []
  if random.random() < 0.5:
    extra_args += ['--no-math64']
  extra_args += ['--no-bitfields'] # due to pnacl bug 4027, "LLVM ERROR: can't convert calls with illegal types"
  # if random.random() < 0.5: extra_args += ['--float'] # XXX hits undefined behavior on float=>int conversions (too big to fit)
  if random.random() < 0.5:
    extra_args += ['--max-funcs', str(random.randint(10, 30))]
  suffix = '.c'
  COMP = shared.CLANG_CC
  fullname = filename + suffix
  check_call([CSMITH, '--no-volatiles', '--no-packed-struct'] + extra_args,
             # ['--max-block-depth', '2', '--max-block-size', '2', '--max-expr-complexity', '2', '--max-funcs', '2'],
             stdout=open(fullname, 'w'))
  print('1) Generate source... %.2f K' % (len(open(fullname).read()) / 1024.))

  tried += 1

  print('2) Compile natively')
  shared.try_delete(filename)
  try:
    shared.run_process([COMP, '-m32', opts, fullname, '-o', filename + '1'] + CSMITH_CFLAGS + ['-w']) # + shared.get_cflags()
  except CalledProcessError:
    print('Failed to compile natively using clang')
    notes['invalid'] += 1
    continue

  shared.run_process([COMP, '-m32', opts, '-emit-llvm', '-c', fullname, '-o', filename + '.bc'] + CSMITH_CFLAGS + ['-w'])
  shared.run_process([COMP, fullname, '-o', filename + '3'] + CSMITH_CFLAGS + ['-w'])
  print('3) Run natively')
  try:
    correct1 = subprocess.run([filename + '1'], stdout=PIPE, stderr=STDOUT, timeout=3)
    if b'Segmentation fault' in correct1.stdout or len(correct1.stdout) < 10:
      raise Exception('segfault')
    correct3 = subprocess.run([filename + '3'], stdout=PIPE, stderr=STDOUT, timeout=3)
    if b'Segmentation fault' in correct3.stdout or len(correct3.stdout) < 10:
      raise Exception('segfault')
    if correct1.stdout != correct3.stdout:
      raise Exception('clang opts change result')
  except Exception as e:
    print('Failed or infinite looping in native, skipping', e)
    notes['invalid'] += 1
    continue

  fail_output_name = 'newfail_%d_%d%s' % (os.getpid(), fails, suffix)

  print('4) Compile JS-ly and compare')

  def try_js(args=[]):
    shared.try_delete(filename + '.js')
    js_args = [shared.EMCC, fullname, '-o', filename + '.js'] + [opts] + CSMITH_CFLAGS + args + ['-w']
    if TEST_BINARYEN:
      if random.random() < 0.5:
        js_args += ['-g']
      if random.random() < 0.5:
        # pick random passes
        BINARYEN_EXTRA_PASSES = [
          "code-pushing",
          "duplicate-function-elimination",
          "dce",
          "remove-unused-brs",
          "remove-unused-names",
          "local-cse",
          "optimize-instructions",
          "post-emscripten",
          "precompute",
          "simplify-locals",
          "simplify-locals-nostructure",
          "vacuum",
          "coalesce-locals",
          "reorder-locals",
          "merge-blocks",
          "remove-unused-module-elements",
          "memory-packing",
        ]
        passes = []
        while 1:
          passes.append(random.choice(BINARYEN_EXTRA_PASSES))
          if random.random() < 0.1:
            break
        js_args += ['-s', 'BINARYEN_EXTRA_PASSES="' + ','.join(passes) + '"']
    if random.random() < 0.5:
      js_args += ['-s', 'ALLOW_MEMORY_GROWTH=1']
    if random.random() < 0.5 and 'ALLOW_MEMORY_GROWTH=1' not in js_args and 'BINARYEN=1' not in js_args:
      js_args += ['-s', 'MAIN_MODULE=1']
    if random.random() < 0.25:
      js_args += ['-s', 'INLINING_LIMIT=1'] # inline nothing, for more call interaction
    if random.random() < 0.5:
      js_args += ['-s', 'ASSERTIONS=1']
    print('(compile)', ' '.join(js_args))
    short_args = [shared.EMCC, fail_output_name] + js_args[5:]
    escaped_short_args = map(lambda x: ("'" + x + "'") if '"' in x else x, short_args)
    open(fullname, 'a').write('\n// ' + ' '.join(escaped_short_args) + '\n\n')
    try:
      shared.run_process(js_args)
      assert os.path.exists(filename + '.js')
      return js_args
    except Exception:
      return False

  def execute_js(engine):
    print('(run in %s)' % engine)
    try:
      js = subprocess.run(shared.NODE_JS + [filename + '.js'], stdout=PIPE, stderr=PIPE, timeout=15 * 60)
    except Exception:
      print('failed to run in primary')
      return False
    js = js.split('\n')[0] + '\n' # remove any extra printed stuff (node workarounds)
    return correct1.stdout == js

  def fail():
    global fails
    print("EMSCRIPTEN BUG")
    notes['embug'] += 1
    fails += 1
    shutil.copyfile(fullname, fail_output_name)

  js_args = try_js()
  if not js_args:
    fail()
    continue
  if not execute_js(engine):
    fail()
    continue
