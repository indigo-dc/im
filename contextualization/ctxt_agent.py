#! /usr/bin/env python
# IM - Infrastructure Manager
# Copyright (C) 2011 - GRyCAP - Universitat Politecnica de Valencia
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import re
import time
import logging
import logging.config
import sys
import subprocess
import os
import getpass
import json
import yaml
try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO
import socket
from multiprocessing import Queue

from IM.SSH import SSH, AuthenticationException


class CtxtAgent():

    SSH_WAIT_TIMEOUT = 600
    # This value enables to retry the playbooks to avoid some SSH connectivity problems
    # The minimum value is 1. This value will be in the data file generated by
    # the ConfManager
    PLAYBOOK_RETRIES = 1

    INTERNAL_PLAYBOOK_RETRIES = 1

    PK_FILE = "/tmp/ansible_key"

    CONF_DATA_FILENAME = None

    logger = None

    @staticmethod
    def wait_winrm_access(vm):
        """
         Test the WinRM access to the VM
        """
        delay = 10
        wait = 0
        last_tested_private = False
        while wait < CtxtAgent.SSH_WAIT_TIMEOUT:
            if 'ctxt_ip' in vm:
                vm_ip = vm['ctxt_ip']
            elif 'private_ip' in vm and not last_tested_private:
                # First test the private one
                vm_ip = vm['private_ip']
                last_tested_private = True
            else:
                vm_ip = vm['ip']
                last_tested_private = False
            try:
                CtxtAgent.logger.debug("Testing WinRM access to VM: " + vm_ip)
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                result = sock.connect_ex((vm_ip, vm['remote_port']))
            except:
                CtxtAgent.logger.exception("Error connecting with WinRM with: " + vm_ip)
                result = -1

            if result == 0:
                vm['ctxt_ip'] = vm_ip
                return True
            else:
                wait += delay
                time.sleep(delay)

    @staticmethod
    def test_ssh(vm, vm_ip, remote_port, delay=10):
        success = False
        CtxtAgent.logger.debug("Testing SSH access to VM: %s:%s" % (vm_ip, remote_port))
        try:
            ssh_client = SSH(vm_ip, vm['user'], vm['passwd'], vm['private_key'], remote_port)
            success = ssh_client.test_connectivity(delay)
            res = 'init'
        except AuthenticationException:
            try_ansible_key = True
            if 'new_passwd' in vm:
                try_ansible_key = False
                # If the process of changing credentials has finished in the
                # VM, we must use the new ones
                CtxtAgent.logger.debug("Error connecting with SSH with initial credentials with: " +
                                       vm_ip + ". Try to use new ones.")
                try:
                    ssh_client = SSH(vm_ip, vm['user'], vm['new_passwd'], vm['private_key'], remote_port)
                    success = ssh_client.test_connectivity()
                    res = "new"
                except AuthenticationException:
                    try_ansible_key = True

            if try_ansible_key:
                # In some very special cases the last two cases fail, so check
                # if the ansible key works
                CtxtAgent.logger.debug("Error connecting with SSH with initial credentials with: " +
                                       vm_ip + ". Try to ansible_key.")
                try:
                    ssh_client = SSH(vm_ip, vm['user'], None, CtxtAgent.PK_FILE, remote_port)
                    success = ssh_client.test_connectivity()
                    res = 'pk_file'
                except:
                    CtxtAgent.logger.exception("Error connecting with SSH with: " + vm_ip)
                    success = False

        return success, res

    @staticmethod
    def wait_ssh_access(vm):
        """
         Test the SSH access to the VM
        """
        delay = 10
        wait = 0
        success = False
        res = None
        while wait < CtxtAgent.SSH_WAIT_TIMEOUT:
            if 'ctxt_ip' in vm and 'ctxt_port' in vm:
                # These have been previously tested and worked use it
                vm_ip = vm['ctxt_ip']
                remote_port = vm['ctxt_port']
                success, res = CtxtAgent.test_ssh(vm, vm['ctxt_ip'], vm['ctxt_port'])
            else:
                # First test the private one
                if 'private_ip' in vm:
                    vm_ip = vm['private_ip']
                    remote_port = vm['remote_port']
                    success, res = CtxtAgent.test_ssh(vm, vm_ip, remote_port)
                    if not success and remote_port != 22:
                        remote_port = 22
                        success, res = CtxtAgent.test_ssh(vm, vm_ip, 22)

                # if not use the default one
                if not success:
                    vm_ip = vm['ip']
                    remote_port = vm['remote_port']
                    success, res = CtxtAgent.test_ssh(vm, vm_ip, remote_port)
                    if not success and remote_port != 22:
                        remote_port = 22
                        success, res = CtxtAgent.test_ssh(vm, vm_ip, remote_port)

                # if not use the default one
                if not success and 'reverse_port' in vm:
                    vm_ip = '127.0.0.1'
                    remote_port = vm['reverse_port']
                    success, res = CtxtAgent.test_ssh(vm, vm_ip, remote_port)

            wait += delay

            if success:
                vm['ctxt_ip'] = vm_ip
                vm['ctxt_port'] = remote_port
                return res
            else:
                time.sleep(delay)

        return None

    @staticmethod
    def run_command(command, timeout=None, poll_delay=5):
        """
         Function to run a command
        """
        try:
            p = subprocess.Popen(command, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, shell=True)

            if timeout is not None:
                wait = 0
                while p.poll() is None and wait < timeout:
                    time.sleep(poll_delay)
                    wait += poll_delay

                if p.poll() is None:
                    p.kill()
                    return "TIMEOUT"

            (out, err) = p.communicate()

            if p.returncode != 0:
                return "ERROR: " + err + out
            else:
                return out
        except Exception as ex:
            return "ERROR: Exception msg: " + str(ex)

    @staticmethod
    def wait_thread(thread_data, output=None):
        """
         Wait for a thread to finish
        """
        thread, result = thread_data
        thread.join()
        try:
            _, (return_code, hosts_with_errors), _ = result.get(timeout=60)
        except:
            CtxtAgent.logger.exception('Error getting ansible results.')
            return_code = -1
            hosts_with_errors = []

        if output:
            if return_code == 0:
                CtxtAgent.logger.info(output)
            else:
                CtxtAgent.logger.error(output)

        return (return_code == 0, hosts_with_errors)

    @staticmethod
    def LaunchAnsiblePlaybook(output, remote_dir, playbook_file, vm, threads, inventory_file, pk_file,
                              retries, change_pass_ok, vault_pass):
        CtxtAgent.logger.debug('Call Ansible')

        extra_vars = {'IM_HOST': vm['ip'] + "_" + str(vm['id'])}
        user = None
        if vm['os'] == "windows":
            gen_pk_file = None
            passwd = vm['passwd']
            if 'new_passwd' in vm and vm['new_passwd'] and change_pass_ok:
                passwd = vm['new_passwd']
        else:
            passwd = vm['passwd']
            if 'new_passwd' in vm and vm['new_passwd'] and change_pass_ok:
                passwd = vm['new_passwd']
            if pk_file:
                gen_pk_file = pk_file
            else:
                if vm['private_key'] and not vm['passwd']:
                    gen_pk_file = "/tmp/pk_" + vm['ip'] + ".pem"
                    pk_out = open(gen_pk_file, 'w')
                    pk_out.write(vm['private_key'])
                    pk_out.close()
                    os.chmod(gen_pk_file, 0o600)
                else:
                    gen_pk_file = None

        # Set local_tmp dir different for any VM
        os.environ['DEFAULT_LOCAL_TMP'] = remote_dir + "/.ansible_tmp"
        # it must be set before doing the import
        from IM.ansible_utils.ansible_launcher import AnsibleThread

        result = Queue()
        t = AnsibleThread(result, output, playbook_file, None, threads, gen_pk_file,
                          passwd, retries, inventory_file, user, vault_pass, extra_vars)
        t.start()
        return (t, result)

    @staticmethod
    def changeVMCredentials(vm, pk_file):
        if vm['os'] == "windows":
            if 'passwd' in vm and vm['passwd'] and 'new_passwd' in vm and vm['new_passwd']:
                try:
                    import winrm
                except:
                    CtxtAgent.logger.exception("Error importing winrm.")
                    return False
                try:
                    url = "https://" + vm['ip'] + ":5986"
                    s = winrm.Session(url, auth=(vm['user'], vm['passwd']), server_cert_validation='ignore')
                    r = s.run_cmd('net', ['user', vm['user'], vm['new_passwd']])

                    # this part of the code is never reached ...
                    if r.status_code == 0:
                        vm['passwd'] = vm['new_passwd']
                        return True
                    else:
                        CtxtAgent.logger.error(
                            "Error changing password to Windows VM: " + r.std_out)
                        return False
                except winrm.exceptions.AuthenticationError:
                    # if the password is correctly changed the command returns this
                    # error
                    try:
                        # let's check that the new password works
                        s = winrm.Session(url, auth=(vm['user'], vm['new_passwd']), server_cert_validation='ignore')
                        r = s.run_cmd('echo', ['OK'])
                        if r.status_code == 0:
                            vm['passwd'] = vm['new_passwd']
                            return True
                        else:
                            CtxtAgent.logger.error(
                                "Error changing password to Windows VM: " + r.std_out)
                            return False
                    except:
                        CtxtAgent.logger.exception(
                            "Error changing password to Windows VM: " + vm['ip'] + ".")
                        return False
                except:
                    CtxtAgent.logger.exception(
                        "Error changing password to Windows VM: " + vm['ip'] + ".")
                    return False
        else:  # Linux VMs
            # Check if we must change user credentials in the VM
            if 'passwd' in vm and vm['passwd'] and 'new_passwd' in vm and vm['new_passwd']:
                CtxtAgent.logger.info("Changing password to VM: " + vm['ip'])
                private_key = vm['private_key']
                if pk_file:
                    private_key = pk_file
                try:
                    ssh_client = SSH(vm['ctxt_ip'], vm['user'], vm['passwd'],
                                     private_key, vm['ctxt_port'])

                    sudo_pass = ""
                    if ssh_client.password:
                        sudo_pass = "echo '" + ssh_client.password + "' | "
                    (out, err, code) = ssh_client.execute(sudo_pass + 'sudo -S bash -c \'echo "' +
                                                          vm['user'] + ':' + vm['new_passwd'] +
                                                          '" | /usr/sbin/chpasswd && echo "OK"\' 2> /dev/null')
                except:
                    CtxtAgent.logger.exception(
                        "Error changing password to VM: " + vm['ip'] + ".")
                    return False

                if code == 0:
                    vm['passwd'] = vm['new_passwd']
                    return True
                else:
                    CtxtAgent.logger.error("Error changing password to VM: " +
                                           vm['ip'] + ". " + out + err)
                    return False

            if 'new_public_key' in vm and vm['new_public_key'] and 'new_private_key' in vm and vm['new_private_key']:
                CtxtAgent.logger.info("Changing public key to VM: " + vm['ip'])
                private_key = vm['private_key']
                if pk_file:
                    private_key = pk_file
                try:
                    ssh_client = SSH(vm['ctxt_ip'], vm['user'], vm[
                                     'passwd'], private_key, vm['ctxt_port'])
                    (out, err, code) = ssh_client.execute_timeout('echo ' + vm['new_public_key'] +
                                                                  ' >> .ssh/authorized_keys', 5)
                except:
                    CtxtAgent.logger.exception(
                        "Error changing public key to VM: " + vm['ip'] + ".")
                    return False

                if code != 0:
                    CtxtAgent.logger.error("Error changing public key to VM:: " +
                                           vm['ip'] + ". " + out + err)
                    return False
                else:
                    vm['private_key'] = vm['new_private_key']
                    return True

        return False

    @staticmethod
    def removeRequiretty(vm, pk_file):
        if not vm['master']:
            CtxtAgent.logger.info("Removing requiretty to VM: " + vm['ip'])
            try:
                private_key = vm['private_key']
                if pk_file:
                    private_key = pk_file
                ssh_client = SSH(vm['ctxt_ip'], vm['user'], vm['passwd'],
                                 private_key, vm['ctxt_port'])
                # Activate tty mode to avoid some problems with sudo in REL
                ssh_client.tty = True
                sudo_pass = ""
                if ssh_client.password:
                    sudo_pass = "echo '" + ssh_client.password + "' | "
                res = ssh_client.execute_timeout(
                    sudo_pass + "sudo -S sed -i 's/.*requiretty$/#Defaults requiretty/' /etc/sudoers", 5)
                if res is not None:
                    (stdout, stderr, code) = res
                    CtxtAgent.logger.debug("OUT: " + stdout + stderr)
                    return code == 0
                else:
                    CtxtAgent.logger.error("No output.")
                    return False
            except:
                CtxtAgent.logger.exception("Error removing requiretty to VM: " + vm['ip'])
                return False
        else:
            return True

    @staticmethod
    def replace_vm_ip(vm_data):
        # Add the Ctxt IP with the one that is actually working
        # in the inventory and in the general info file
        with open(CtxtAgent.CONF_DATA_FILENAME) as f:
            general_conf_data = json.load(f)

        for vm in general_conf_data['vms']:
            if vm['id'] == vm_data['id']:
                vm['ctxt_ip'] = vm_data['ctxt_ip']
                vm['ctxt_port'] = vm_data['ctxt_port']

        with open(CtxtAgent.CONF_DATA_FILENAME, 'w+') as f:
            json.dump(general_conf_data, f, indent=2)

        # Now in the ansible inventory
        filename = general_conf_data['conf_dir'] + "/hosts"
        with open(filename) as f:
            inventoy_data = ""
            for line in f:
                if line.startswith("%s_%s " % (vm_data['ip'], vm_data['id'])):
                    line = re.sub(" ansible_host=%s " % vm_data['ip'],
                                  " ansible_host=%s " % vm_data['ctxt_ip'], line)
                    line = re.sub(" ansible_ssh_host=%s " % vm_data['ip'],
                                  " ansible_ssh_host=%s " % vm_data['ctxt_ip'], line)
                    line = re.sub(" ansible_port=%s " % vm_data['remote_port'],
                                  " ansible_port=%s " % vm_data['ctxt_port'], line)
                    line = re.sub(" ansible_ssh_port=%s " % vm_data['remote_port'],
                                  " ansible_ssh_port=%s " % vm_data['ctxt_port'], line)
                inventoy_data += line

        with open(filename, 'w+') as f:
            f.write(inventoy_data)

    @staticmethod
    def install_ansible_modules(general_conf_data, playbook):
        new_playbook = playbook
        if 'ansible_modules' in general_conf_data and general_conf_data['ansible_modules']:
            play_dir = os.path.dirname(playbook)
            play_filename = os.path.basename(playbook)
            new_playbook = os.path.join(play_dir, "mod_" + play_filename)

            with open(playbook) as f:
                yaml_data = yaml.load(f)

            galaxy_dependencies = []
            needs_git = False
            for galaxy_name in general_conf_data['ansible_modules']:
                galaxy_name = galaxy_name.encode()
                if galaxy_name:
                    CtxtAgent.logger.debug("Install " + galaxy_name + " with ansible-galaxy.")

                    if galaxy_name.startswith("git"):
                        needs_git = True

                    parts = galaxy_name.split("|")
                    if len(parts) > 1:
                        url = parts[0]
                        rolename = parts[1]
                        dep = {"src": url, "name": rolename}
                    else:
                        url = rolename = galaxy_name
                        dep = {"src": url}

                    parts = url.split(",")
                    if len(parts) > 1:
                        url = parts[0]
                        version = parts[1]
                        dep = {"src": url, "version": version}

                    galaxy_dependencies.append(dep)

            if needs_git:
                task = {"yum": "name=git"}
                task["name"] = "Install git with yum"
                task["become"] = "yes"
                task["when"] = 'ansible_os_family == "RedHat"'
                yaml_data[0]['tasks'].append(task)
                task = {"apt": "name=git"}
                task["name"] = "Install git with apt"
                task["become"] = "yes"
                task["when"] = 'ansible_os_family == "Debian"'
                yaml_data[0]['tasks'].append(task)

            if galaxy_dependencies:
                now = str(int(time.time() * 100))
                filename = "/tmp/galaxy_roles_%s.yml" % now
                yaml_deps = yaml.dump(galaxy_dependencies, indent=2)
                CtxtAgent.logger.debug("Galaxy depencies file: %s" % yaml_deps)
                task = {"copy": 'dest=%s content="%s"' % (filename, yaml_deps)}
                task["name"] = "Create YAML file to install the roles with ansible-galaxy"
                yaml_data[0]['tasks'].append(task)

                task = {"command": "ansible-galaxy install -r %s" % filename}
                task["name"] = "Install galaxy roles"
                task["become"] = "yes"
                yaml_data[0]['tasks'].append(task)

            with open(new_playbook, 'w+') as f:
                yaml.dump(yaml_data, f)

        return new_playbook

    @staticmethod
    def contextualize_vm(general_conf_data, vm_conf_data):
        vault_pass = None
        if 'VAULT_PASS' in os.environ:
            vault_pass = os.environ['VAULT_PASS']

        res_data = {}
        CtxtAgent.logger.info('Generate and copy the ssh key')

        # If the file exists, do not create it again
        if not os.path.isfile(CtxtAgent.PK_FILE):
            out = CtxtAgent.run_command('ssh-keygen -t rsa -C ' + getpass.getuser() +
                                        ' -q -N "" -f ' + CtxtAgent.PK_FILE)
            CtxtAgent.logger.debug(out)

        ctxt_vm = None
        for vm in general_conf_data['vms']:
            if vm['id'] == vm_conf_data['id']:
                ctxt_vm = vm

        if not ctxt_vm:
            CtxtAgent.logger.error("No VM to Contextualize!")
            res_data['OK'] = True
            return res_data

        for task in vm_conf_data['tasks']:
            task_ok = False
            num_retries = 0
            while not task_ok and num_retries < CtxtAgent.PLAYBOOK_RETRIES:
                num_retries += 1
                CtxtAgent.logger.info('Launch task: ' + task)
                if ctxt_vm['os'] == "windows":
                    # playbook = general_conf_data['conf_dir'] + "/" + task + "_task_all_win.yml"
                    playbook = general_conf_data[
                        'conf_dir'] + "/" + task + "_task.yml"
                else:
                    playbook = general_conf_data[
                        'conf_dir'] + "/" + task + "_task_all.yml"
                inventory_file = general_conf_data['conf_dir'] + "/hosts"

                ansible_thread = None
                if task == "wait_all_ssh":
                    # Wait all the VMs to have remote access active
                    for vm in general_conf_data['vms']:
                        if vm['os'] == "windows":
                            CtxtAgent.logger.info("Waiting WinRM access to VM: " + vm['ip'])
                            cred_used = CtxtAgent.wait_winrm_access(vm)
                        else:
                            CtxtAgent.logger.info("Waiting SSH access to VM: " + vm['ip'])
                            cred_used = CtxtAgent.wait_ssh_access(vm)

                        if not cred_used:
                            CtxtAgent.logger.error("Error Waiting access to VM: " + vm['ip'])
                            res_data['SSH_WAIT'] = False
                            res_data['OK'] = False
                            return res_data
                        else:
                            res_data['SSH_WAIT'] = True
                            CtxtAgent.logger.info("Remote access to VM: " + vm['ip'] + " Open!")

                        # the IP has changed public for private
                        if 'ctxt_ip' in vm and vm['ctxt_ip'] != vm['ip']:
                            # update the ansible inventory
                            CtxtAgent.logger.info("Changing the IP %s for %s in config files." % (vm['ctxt_ip'],
                                                                                                  vm['ip']))
                            CtxtAgent.replace_vm_ip(vm)
                elif task == "basic":
                    # This is always the fist step, so put the SSH test, the
                    # requiretty removal and change password here
                    if ctxt_vm['os'] == "windows":
                        CtxtAgent.logger.info("Waiting WinRM access to VM: " + ctxt_vm['ip'])
                        cred_used = CtxtAgent.wait_winrm_access(ctxt_vm)
                    else:
                        CtxtAgent.logger.info("Waiting SSH access to VM: " + ctxt_vm['ip'])
                        cred_used = CtxtAgent.wait_ssh_access(ctxt_vm)

                    if not cred_used:
                        CtxtAgent.logger.error("Error Waiting access to VM: " + ctxt_vm['ip'])
                        res_data['SSH_WAIT'] = False
                        res_data['OK'] = False
                        return res_data
                    else:
                        res_data['SSH_WAIT'] = True
                        CtxtAgent.logger.info("Remote access to VM: " + ctxt_vm['ip'] + " Open!")

                    # The basic task uses the credentials of VM stored in ctxt_vm
                    pk_file = None
                    if cred_used == "pk_file":
                        pk_file = CtxtAgent.PK_FILE

                    # First remove requiretty in the node
                    if ctxt_vm['os'] != "windows":
                        success = CtxtAgent.removeRequiretty(ctxt_vm, pk_file)
                        if success:
                            CtxtAgent.logger.info("Requiretty successfully removed")
                        else:
                            CtxtAgent.logger.error("Error removing Requiretty")

                    # Check if we must change user credentials
                    # Do not change it on the master. It must be changed only by
                    # the ConfManager
                    change_creds = False
                    if not ctxt_vm['master']:
                        change_creds = CtxtAgent.changeVMCredentials(ctxt_vm, pk_file)
                        res_data['CHANGE_CREDS'] = change_creds

                    if ctxt_vm['os'] != "windows":
                        if ctxt_vm['master']:
                            # Install ansible modules
                            playbook = CtxtAgent.install_ansible_modules(general_conf_data, playbook)
                        # this step is not needed in windows systems
                        ansible_thread = CtxtAgent.LaunchAnsiblePlaybook(CtxtAgent.logger, vm_conf_data['remote_dir'],
                                                                         playbook, ctxt_vm, 2, inventory_file,
                                                                         pk_file, CtxtAgent.INTERNAL_PLAYBOOK_RETRIES,
                                                                         change_creds, vault_pass)
                else:
                    # in the other tasks pk_file can be used
                    ansible_thread = CtxtAgent.LaunchAnsiblePlaybook(CtxtAgent.logger, vm_conf_data['remote_dir'],
                                                                     playbook, ctxt_vm, 2,
                                                                     inventory_file, CtxtAgent.PK_FILE,
                                                                     CtxtAgent.INTERNAL_PLAYBOOK_RETRIES,
                                                                     vm_conf_data['changed_pass'], vault_pass)

                if ansible_thread:
                    (task_ok, _) = CtxtAgent.wait_thread(ansible_thread)
                else:
                    task_ok = True
                if not task_ok:
                    CtxtAgent.logger.warn("ERROR executing task %s: (%s/%s)" %
                                          (task, num_retries, CtxtAgent.PLAYBOOK_RETRIES))
                else:
                    CtxtAgent.logger.info('Task %s finished successfully' % task)

            res_data[task] = task_ok
            if not task_ok:
                res_data['OK'] = False
                return res_data

        res_data['OK'] = True

        CtxtAgent.logger.info('Process finished')
        return res_data

    @staticmethod
    def run(general_conf_file, vm_conf_file):
        CtxtAgent.CONF_DATA_FILENAME = general_conf_file

        with open(CtxtAgent.CONF_DATA_FILENAME) as f:
            general_conf_data = json.load(f)
        with open(vm_conf_file) as f:
            vm_conf_data = json.load(f)

        # Root logger: is used by paramiko
        logging.basicConfig(filename=vm_conf_data['remote_dir'] + "/ctxt_agent.log",
                            level=logging.WARNING,
                            # format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                            format='%(message)s',
                            datefmt='%m-%d-%Y %H:%M:%S')
        # ctxt_agent logger
        CtxtAgent.logger = logging.getLogger('ctxt_agent')
        CtxtAgent.logger.setLevel(logging.DEBUG)

        if 'playbook_retries' in general_conf_data:
            CtxtAgent.PLAYBOOK_RETRIES = general_conf_data['playbook_retries']

        CtxtAgent.PK_FILE = general_conf_data['conf_dir'] + "/" + "ansible_key"

        res_data = CtxtAgent.contextualize_vm(general_conf_data, vm_conf_data)

        ctxt_out = open(vm_conf_data['remote_dir'] + "/ctxt_agent.out", 'w')
        json.dump(res_data, ctxt_out, indent=2)
        ctxt_out.close()

        return res_data['OK']


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Contextualization Agent.')
    parser.add_argument('general', type=str, nargs=1)
    parser.add_argument('vmconf', type=str, nargs=1)
    options = parser.parse_args()

    if CtxtAgent.run(options.general[0], options.vmconf[0]):
        sys.exit(0)
    else:
        sys.exit(1)
