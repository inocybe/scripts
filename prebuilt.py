#!/usr/bin/python
# Copyright (c) 2010 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import datetime
import multiprocessing
import optparse
import os
import re
import sys

from chromite.lib import cros_build_lib
"""
This script is used to upload host prebuilts as well as board BINHOSTS to
Google Storage.

After a build is successfully uploaded a file is updated with the proper
BINHOST version as well as the target board. This file is defined in GIT_FILE


To read more about prebuilts/binhost binary packages please refer to:
http://sites/chromeos/for-team-members/engineering/releng/prebuilt-binaries-for-streamlining-the-build-process


Example of uploading prebuilt amd64 host files
./prebuilt.py -p /b/cbuild/build -s -u gs://chromeos-prebuilt

Example of uploading x86-dogfood binhosts
./prebuilt.py -b x86-dogfood -p /b/cbuild/build/ -u gs://chromeos-prebuilt  -g
"""

# as per http://crosbug.com/5855 always filter the below packages
_FILTER_PACKAGES = set()
_RETRIES = 3
_GSUTIL_BIN = '/b/third_party/gsutil/gsutil'
_HOST_PACKAGES_PATH = 'chroot/var/lib/portage/pkgs'
_HOST_TARGET = 'amd64'
_BOARD_PATH = 'chroot/build/%(board)s'
_BOTO_CONFIG = '/home/chrome-bot/external-boto'
# board/board-target/version'
_GS_BOARD_PATH = 'board/%(board)s/%(version)s/'
# We only support amd64 right now
_GS_HOST_PATH = 'host/%s' % _HOST_TARGET
# Private overlays to look at for builds to filter
# relative to build path
_PRIVATE_OVERLAY_DIR = 'src/private-overlays'
_BINHOST_BASE_DIR = 'src/overlays'
_BINHOST_BASE_URL = 'http://commondatastorage.googleapis.com/chromeos-prebuilt'
_PREBUILT_BASE_DIR = 'src/third_party/chromiumos-overlay/chromeos/config/'
# Created in the event of new host targets becoming available
_PREBUILT_MAKE_CONF = {'amd64': os.path.join(_PREBUILT_BASE_DIR,
                                             'make.conf.amd64-host')}


class FiltersEmpty(Exception):
  """Raised when filters are used but none are found."""
  pass


class UploadFailed(Exception):
  """Raised when one of the files uploaded failed."""
  pass

class UnknownBoardFormat(Exception):
  """Raised when a function finds an unknown board format."""
  pass


def UpdateLocalFile(filename, value, key='PORTAGE_BINHOST'):
  """Update the key in file with the value passed.
  File format:
    key="value"
  Note quotes are added automatically

  Args:
    filename: Name of file to modify.
    value: Value to write with the key.
    key: The variable key to update. (Default: PORTAGE_BINHOST)
  """
  file_fh = open(filename)
  file_lines = []
  found = False
  for line in file_fh:
    if '=' not in line:
      # Skip any line without an equal in it and just write it out
      file_lines.append(line)
      continue

    file_var, file_val = line.split('=')
    keyval_str = '%(key)s=%(value)s'
    if file_var == key:
      found = True
      print 'Updating %s=%s to %s="%s"' % (file_var, file_val, key, value)
      value = '"%s"' % value
      file_lines.append(keyval_str % {'key': key, 'value': value})
    else:
      file_lines.append(keyval_str % {'key': file_var, 'value': file_val})

  if not found:
    file_lines.append(keyval_str % {'key': key, 'value': value})

  file_fh.close()
  # write out new file
  new_file_fh = open(filename, 'w')
  new_file_fh.write('\n'.join(file_lines))
  new_file_fh.close()


def RevGitFile(filename, value):
  """Update and push the git file.

  Args:
    filename: file to modify that is in a git repo already
    key: board or host package type e.g. x86-dogfood
    value: string representing the version of the prebuilt that has been
      uploaded.
  """
  prebuilt_branch = 'prebuilt_branch'
  old_cwd = os.getcwd()
  os.chdir(os.path.dirname(filename))

  cros_build_lib.RunCommand('repo sync', shell=True)
  cros_build_lib.RunCommand('repo start %s  .' % prebuilt_branch, shell=True)
  git_ssh_config_cmd = (
    'git config url.ssh://git@gitrw.chromium.org:9222.pushinsteadof '
    'http://git.chromium.org/git')
  cros_build_lib.RunCommand(git_ssh_config_cmd, shell=True)
  description = 'Update PORTAGE_BINHOST="%s" in %s' % (value, file)
  print description
  try:
    UpdateLocalFile(filename, value)
    cros_build_lib.RunCommand('git config push.default tracking', shell=True)
    cros_build_lib.RunCommand('git commit -am "%s"' % description, shell=True)
    cros_build_lib.RunCommand('repo sync', shell=True)
    cros_build_lib.RunCommand('git push', shell=True)
  finally:
    cros_build_lib.RunCommand('repo abandon %s .' % prebuilt_branch, shell=True)
    os.chdir(old_cwd)


def GetVersion():
  """Get the version to put in LATEST and update the git version with."""
  return datetime.datetime.now().strftime('%d.%m.%y.%H%M%S')


def LoadPrivateFilters(build_path):
  """Load private filters based on ebuilds found under _PRIVATE_OVERLAY_DIR.

  This function adds filters to the global set _FILTER_PACKAGES.
  Args:
    build_path: Path that _PRIVATE_OVERLAY_DIR is in.
  """
  # TODO(scottz): eventually use manifest.xml to find the proper
  # private overlay path.
  filter_path = os.path.join(build_path, _PRIVATE_OVERLAY_DIR)
  files = cros_build_lib.ListFiles(filter_path)
  filters = []
  for file in files:
    if file.endswith('.ebuild'):
      basename = os.path.basename(file)
      match = re.match('(.*?)-\d.*.ebuild', basename)
      if match:
        filters.append(match.group(1))

  if not filters:
    raise FiltersEmpty('No filters were returned')

  _FILTER_PACKAGES.update(filters)


def ShouldFilterPackage(file_path):
  """Skip a particular file if it matches a pattern.

  Skip any files that machine the list of packages to filter in
  _FILTER_PACKAGES.

  Args:
    file_path: string of a file path to inspect against _FILTER_PACKAGES

  Returns:
    True if we should filter the package,
    False otherwise.
  """
  for name in _FILTER_PACKAGES:
    if name in file_path:
      print 'FILTERING %s' % file_path
      return True

  return False


def _GsUpload(args):
  """Upload to GS bucket.

  Args:
    args: a tuple of two arguments that contains local_file and remote_file.

  Returns:
    Return the arg tuple of two if the upload failed
  """
  (local_file, remote_file) = args
  if ShouldFilterPackage(local_file):
    return

  cmd = '%s cp -a public-read %s %s' % (_GSUTIL_BIN, local_file, remote_file)
  # TODO(scottz): port to use _Run or similar when it is available in
  # cros_build_lib.
  for attempt in range(_RETRIES):
    try:
      output = cros_build_lib.RunCommand(cmd, print_cmd=False, shell=True)
      break
    except cros_build_lib.RunCommandError:
      print 'Failed to sync %s -> %s, retrying' % (local_file, remote_file)
  else:
    # TODO(scottz): potentially return what failed so we can do something with
    # with it but for now just print an error.
    print 'Retry failed uploading %s -> %s, giving up' % (local_file,
                                                          remote_file)
    return args


def RemoteUpload(files, pool=10):
  """Upload to google storage.

  Create a pool of process and call _GsUpload with the proper arguments.

  Args:
    files: dictionary with keys to local files and values to remote path.
    pool: integer of maximum proesses to have at the same time.

  Returns:
    Return a set of tuple arguments of the failed uploads
  """
  # TODO(scottz) port this to use _RunManyParallel when it is available in
  # cros_build_lib
  pool = multiprocessing.Pool(processes=pool)
  workers = []
  for local_file, remote_path in files.iteritems():
    workers.append((local_file, remote_path))

  result = pool.map_async(_GsUpload, workers, chunksize=1)
  while True:
    try:
      return set(result.get(60*60))
    except multiprocessing.TimeoutError:
      pass


def GenerateUploadDict(local_path, gs_path, strip_str):
  """Build a dictionary of local remote file key pairs for gsutil to upload.

  Args:
    local_path: A path to the file on the local hard drive.
    gs_path: Path to upload in Google Storage.
    strip_str: String to remove from the local_path so that the relative
      file path can be tacked on to the gs_path.

  Returns:
    Returns a dictionary of file path/gs_dest_path pairs
  """
  files_to_sync = cros_build_lib.ListFiles(local_path)
  upload_files = {}
  for file_path in files_to_sync:
    filename = file_path.replace(strip_str, '').lstrip('/')
    gs_file_path = os.path.join(gs_path, filename)
    upload_files[file_path] = gs_file_path

  return upload_files


def DetermineMakeConfFile(target):
  """Determine the make.conf file that needs to be updated for prebuilts.

    Args:
      target: String representation of the board. This includes host and board
        targets

    Returns
      A string path to a make.conf file to be updated.
  """
  if _HOST_TARGET == target:
    # We are host.
    # Without more examples of hosts this is a kludge for now.
    # TODO(Scottz): as new host targets come online expand this to
    # work more like boards.
    make_path =  _PREBUILT_MAKE_CONF[target]
  elif re.match('.*?-.*?_.*', target):
    # We are a board variant
    overlay_str = 'overlay-variant-%s' % target.replace('_', '-')
    make_path = os.path.join(_BINHOST_BASE_DIR, overlay_str, 'make.conf')
  elif re.match('.*?-\w+', target):
    overlay_str = 'overlay-%s' % target
    make_path = os.path.join(_BINHOST_BASE_DIR, overlay_str, 'make.conf')
  else:
    raise UnknownBoardFormat('Unknown format: %s' % target)

  return os.path.join(make_path)


def UploadPrebuilt(build_path, bucket, version, board=None, git_sync=False):
  """Upload Host prebuilt files to Google Storage space.

  Args:
    build_path: The path to the root of the chroot.
    bucket: The Google Storage bucket to upload to.
    board: The board to upload to Google Storage, if this is None upload
      host packages.
    git_sync: If set, update make.conf of target to reference the latest
      prebuilt packages genereated here.
  """

  if not board:
    # We are uploading host packages
    # TODO(scottz): eventually add support for different host_targets
    package_path = os.path.join(build_path, _HOST_PACKAGES_PATH)
    gs_path = os.path.join(bucket, _GS_HOST_PATH, version)
    strip_pattern = package_path
    package_string = _HOST_TARGET
    git_file = os.path.join(build_path, _PREBUILT_MAKE_CONF[_HOST_TARGET])
    url_suffix = '%s/%s' % (_GS_HOST_PATH, version)
  else:
    board_path = os.path.join(build_path, _BOARD_PATH % {'board': board})
    package_path = os.path.join(board_path, 'packages')
    package_string = board
    strip_pattern = board_path
    remote_board_path = _GS_BOARD_PATH % {'board': board, 'version': version}
    gs_path = os.path.join(bucket, remote_board_path)
    git_file = os.path.join(build_path, DetermineMakeConfFile(board))
    url_suffix = remote_board_path

  upload_files = GenerateUploadDict(package_path, gs_path, strip_pattern)

  print 'Uploading %s' % package_string
  failed_uploads = RemoteUpload(upload_files)
  if len(failed_uploads) > 1 or (None not in failed_uploads):
    error_msg = ['%s -> %s\n' % args for args in failed_uploads]
    raise UploadFailed('Error uploading:\n%s' % error_msg)

  if git_sync:
    url_value = '%s/%s' % (_BINHOST_BASE_URL, url_suffix)
    RevGitFile(git_file, url_value)


def usage(parser, msg):
  """Display usage message and parser help then exit with 1."""
  print >> sys.stderr, msg
  parser.print_help()
  sys.exit(1)


def main():
  parser = optparse.OptionParser()
  parser.add_option('-b', '--board', dest='board', default=None,
                    help='Board type that was built on this machine')
  parser.add_option('-p', '--build-path', dest='build_path',
                    help='Path to the chroot')
  parser.add_option('-s', '--sync-host', dest='sync_host',
                    default=False, action='store_true',
                    help='Sync host prebuilts')
  parser.add_option('-g', '--git-sync', dest='git_sync',
                    default=False, action='store_true',
                    help='Enable git version sync (This commits to a repo)')
  parser.add_option('-u', '--upload', dest='upload',
                    default=None,
                    help='Upload to GS bucket')
  parser.add_option('-V', '--prepend-version', dest='prepend_version',
                    default=None,
                    help='Add an identifier to the front of the version')
  parser.add_option('-f', '--filters', dest='filters', action='store_true',
                    default=False,
                    help='Turn on filtering of private ebuild packages')

  options, args = parser.parse_args()
  # Setup boto environment for gsutil to use
  os.environ['BOTO_CONFIG'] = _BOTO_CONFIG
  if not options.build_path:
    usage(parser, 'Error: you need provide a chroot path')

  if not options.upload:
    usage(parser, 'Error: you need to provide a gsutil upload bucket -u')

  if options.filters:
    LoadPrivateFilters(options.build_path)

  version = GetVersion()
  if options.prepend_version:
    version = '%s-%s' % (options.prepend_version, version)

  if options.sync_host:
    UploadPrebuilt(options.build_path, options.upload, version,
                   git_sync=options.git_sync)

  if options.board:
    UploadPrebuilt(options.build_path, options.upload, version,
                   board=options.board, git_sync=options.git_sync)


if __name__ == '__main__':
  main()