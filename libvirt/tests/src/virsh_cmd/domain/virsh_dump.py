import os
import logging
import commands
import time
import signal
from autotest.client.shared import error
from autotest.client.shared import utils
from autotest.client.shared.error import CmdError
from virttest import virsh
from virttest import utils_libvirtd
from virttest.libvirt_xml import vm_xml
from provider import libvirt_version


def wait_pid_active(pid, timeout=5):
    """
    Wait for pid in running status

    :param: pid: Desired pid
    :param: timeout: Max time we can wait
    """
    cmd = ("cat /proc/%d/stat | awk '{print $3}'" % pid)
    try:
        while (True):
            timeout = timeout - 1
            if not timeout:
                raise error.TestNAError("Time out for waiting pid!")
            pid_status = utils.run(cmd, ignore_status=False).stdout.strip()
            if pid_status != "R":
                time.sleep(1)
                continue
            else:
                break
    except Exception, detail:
        raise error.TestFail(detail)


def check_flag(file_flag):
    """
    Check if file flag include O_DIRECT.

    Note, O_DIRECT is defined as:
    #define O_DIRECT        00040000        /* direct disk access hint */
    """
    if int(file_flag) == 4:
        logging.info("File flags include O_DIRECT")
        return True
    else:
        logging.error("File flags doesn't include O_DIRECT")
        return False


def check_bypass(dump_file):
    """
    Get the file flags of domain core dump file and check it.
    """

    cmd1 = "lsof -w %s" % dump_file
    while True:
        if not os.path.exists(dump_file) or os.system(cmd1):
            continue
        cmd2 = ("cat /proc/$(%s |awk '/libvirt_i/{print $2}')/fdinfo/1"
                "|grep flags|awk '{print $NF}'" % cmd1)
        (status, output) = commands.getstatusoutput(cmd2)
        if status:
            raise error.TestFail("Fail to get the flags of dumped file")
        if not len(output):
            continue
        try:
            logging.debug("The flag of dumped file: %s", output)
            file_flag = output[-5]
            if check_flag(file_flag):
                logging.info("Bypass file system cache "
                             "successfully when dumping")
                break
            else:
                raise error.TestFail("Bypass file system cache "
                                     "fail when dumping")
        except (ValueError, IndexError), detail:
            raise error.TestFail(detail)


def run(test, params, env):
    """
    Test command: virsh dump.

    This command can dump the core of a domain to a file for analysis.
    1. Positive testing
        1.1 Dump domain with valid options.
        1.2 Avoid file system cache when dumping.
        1.3 Compress the dump images to valid/invalid formats.
    2. Negative testing
        2.1 Dump domain to a non-exist directory.
        2.2 Dump domain with invalid option.
        2.3 Dump a shut-off domain.
    """

    vm_name = params.get("main_vm", "vm1")
    vm = env.get_vm(vm_name)
    options = params.get("dump_options")
    dump_file = params.get("dump_file", "vm.core")
    if os.path.dirname(dump_file) is "":
        dump_file = os.path.join(test.tmpdir, dump_file)
    dump_image_format = params.get("dump_image_format")
    start_vm = params.get("start_vm") == "yes"
    paused_after_start_vm = params.get("paused_after_start_vm") == "yes"
    status_error = params.get("status_error", "no") == "yes"
    timeout = int(params.get("timeout", "5"))
    qemu_conf = "/etc/libvirt/qemu.conf"
    uri = params.get("virsh_uri")
    unprivileged_user = params.get('unprivileged_user')
    if unprivileged_user:
        if unprivileged_user.count('EXAMPLE'):
            unprivileged_user = 'testacl'

    if not libvirt_version.version_compare(1, 1, 1):
        if params.get('setup_libvirt_polkit') == 'yes':
            raise error.TestNAError("API acl test not supported in current"
                                    + " libvirt version.")

    def check_domstate(actual, options):
        """
        Check the domain status according to dump options.
        """

        if options.find('live') >= 0:
            domstate = "running"
            if options.find('crash') >= 0 or options.find('reset') > 0:
                domstate = "running"
            if paused_after_start_vm:
                domstate = "paused"
        elif options.find('crash') >= 0:
            domstate = "shut off"
            if options.find('reset') >= 0:
                domstate = "running"
        elif options.find('reset') >= 0:
            domstate = "running"
            if paused_after_start_vm:
                domstate = "paused"
        else:
            domstate = "running"
            if paused_after_start_vm:
                domstate = "paused"

        if not start_vm:
            domstate = "shut off"

        logging.debug("Domain should %s after run dump %s", domstate, options)

        return (domstate == actual)

    def check_dump_format(dump_image_format, dump_file):
        """
        Check the format of dumped file.

        If 'dump_image_format' is not specified or invalid in qemu.conf, then
        the file shoule be normal raw file, otherwise it shoud be compress to
        specified format, the supported compress format including: lzop, gzip,
        bzip2, and xz.
        """

        valid_format = ["lzop", "gzip", "bzip2", "xz"]
        if len(dump_image_format) == 0 or dump_image_format not in valid_format:
            logging.debug("No need check the dumped file format")
            return True
        else:
            file_cmd = "file %s" % dump_file
            (status, output) = commands.getstatusoutput(file_cmd)
            if status:
                logging.error("Fail to check dumped file %s", dump_file)
                return False
            logging.debug("Run file %s output: %s", dump_file, output)
            actual_format = output.split(" ")[1]
            if actual_format.lower() != dump_image_format.lower():
                logging.error("Compress dumped file to %s fail: %s" %
                              (dump_image_format, actual_format))
                return False
            else:
                return True

    # Configure dump_image_format in /etc/libvirt/qemu.conf.
    if len(dump_image_format):
        conf_cmd = ("echo dump_image_format = \\\"%s\\\" >> %s" %
                    (dump_image_format, qemu_conf))
        if os.system(conf_cmd):
            logging.error("Config dump_image_format to %s fail",
                          dump_image_format)
        libvirtd_service = utils_libvirtd.Libvirtd()
        libvirtd_service.restart()
        if not libvirtd_service.is_running:
            raise error.TestNAError("libvirt service is not running!")

    # Deal with bypass-cache option
    child_pid = 0
    if options.find('bypass-cache') >= 0:
        pid = os.fork()
        if pid:
            # Guarantee check_bypass function has run before dump
            child_pid = pid
            try:
                wait_pid_active(pid, timeout)
            finally:
                os.kill(child_pid, signal.SIGUSR1)
        else:
            check_bypass(dump_file)
            # Wait for parent process over
            while True:
                time.sleep(1)

    # Back up xml file
    vmxml = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name)
    backup_xml = vmxml.copy()

    dump_guest_core = params.get("dump_guest_core", "")
    if dump_guest_core not in ["", "on", "off"]:
        raise error.TestError("invalid dumpCore value: %s" % dump_guest_core)
    try:
        # Set dumpCore in guest xml
        if dump_guest_core:
            if vm.is_alive():
                vm.destroy(gracefully=False)
            vmxml.dumpcore = dump_guest_core
            vmxml.sync()
            vm.start()
            # check qemu-kvm cmdline
            vm_pid = vm.get_pid()
            cmd = "cat /proc/%d/cmdline|xargs -0 echo" % vm_pid
            cmd += "|grep dump-guest-core=%s" % dump_guest_core
            result = utils.run(cmd, ignore_status=True)
            logging.debug("cmdline: %s" % result.stdout)
            if result.exit_status:
                error.TestFail("Not find dump-guest-core=%s in qemu cmdline"
                               % dump_guest_core)
            else:
                logging.info("Find dump-guest-core=%s in qemum cmdline",
                             dump_guest_core)

        # Run virsh command
        cmd_result = virsh.dump(vm_name, dump_file, options,
                                unprivileged_user=unprivileged_user,
                                uri=uri,
                                ignore_status=True, debug=True)
        status = cmd_result.exit_status

        logging.info("Start check result")
        if not check_domstate(vm.state(), options):
            raise error.TestFail("Domain status check fail.")
        if status_error:
            if not status:
                raise error.TestFail("Expect fail, but run successfully")
        else:
            if status:
                raise error.TestFail("Expect succeed, but run fail")
            if not os.path.exists(dump_file):
                raise error.TestFail("Fail to find domain dumped file.")
            if check_dump_format(dump_image_format, dump_file):
                logging.info("Successfully dump domain to %s", dump_file)
            else:
                raise error.TestFail("The format of dumped file is wrong.")
    finally:
        if child_pid:
            os.kill(child_pid, signal.SIGUSR1)
        if os.path.isfile(dump_file):
            os.remove(dump_file)
        if len(dump_image_format):
            clean_qemu_conf = "sed -i '$d' %s " % qemu_conf
            if os.system(clean_qemu_conf):
                raise error.TestFail("Fail to recover %s", qemu_conf)
        if vm.is_alive():
            vm.destroy(gracefully=False)
        backup_xml.sync()
