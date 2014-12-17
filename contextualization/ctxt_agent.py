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

from optparse import OptionParser
import time
import logging
import logging.config
import sys, subprocess, os
import getpass
import json
import threading

from SSH import SSH, AuthenticationException
from ansible_launcher import AnsibleThread


SSH_WAIT_TIMEOUT = 600
# This value enables to retry the playbooks to avoid some SSH connectivity problems
# The minimum value is 1. This value will be in the data file generated by the ConfManager
PLAYBOOK_RETRIES = 1


def wait_ssh_access(vm):
	"""
	 Test the SSH access to the VM
	"""
	delay = 10
	wait = 0
	while wait < SSH_WAIT_TIMEOUT:
		logger.debug("Testing SSH access to VM: " + vm['ip'])
		wait += delay
		success = False
		try:
			ssh_client = SSH(vm['ip'], vm['user'], vm['passwd'], vm['private_key'], vm['ssh_port'])
			success = ssh_client.test_connectivity()
		except AuthenticationException:
			# If the process of changing credentials has finished in the VM, we must use the new ones
			if 'new_passwd' in vm:
				logger.warn("Error connecting with SSH with initial credentials with: " + vm['ip'] + ". Try to use new ones.")
				try:
					ssh_client = SSH(vm['ip'], vm['user'], vm['new_passwd'], vm['private_key'], vm['ssh_port'])
					success = ssh_client.test_connectivity()
				except:
					logger.exception("Error connecting with SSH with: " + vm['ip'])
					success = False
			
		if success:
			return True
		else:
			time.sleep(delay)
	
	return False

def run_command(command, timeout = None, poll_delay = 5):
	"""
	 Function to run a command
	"""
	try:
		p=subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
		
		if timeout is not None:
			wait = 0
			while p.poll() is None and wait < timeout:
				time.sleep(poll_delay)
				wait += poll_delay

			if p.poll() is None:
				p.kill()
				return "TIMEOUT"

		(out, err) = p.communicate()
		
		if p.returncode!=0:
			return "ERROR: " + err + out
		else:
			return out
	except Exception, ex:
		return "ERROR: Exception msg: " + str(ex)

def wait_thread(thread):
	"""
	 Wait for a thread to finish
	"""
	thread.join()
	(return_code, output, hosts_with_errors) = thread.results

	if return_code==0:
		logger.debug(output)
	else:
		logger.error(output)

	return (return_code==0, hosts_with_errors)

def LaunchAnsiblePlaybook(playbook_file, vm, threads, inventory_file, pk_file, retries, change_pass_ok):
	logger.debug('Call Ansible')
	
	passwd = None
	if pk_file:
		gen_pk_file = pk_file
	else:
		if vm['private_key'] and not vm['passwd']:
			gen_pk_file = "/tmp/pk_" + vm['ip'] + ".pem"
			# If the file exists do not create it again
			if not os.path.isfile(gen_pk_file):
				pk_out = open(gen_pk_file, 'w')
				pk_out.write(vm['private_key'])
				pk_out.close()
				os.chmod(gen_pk_file,0400)
		else:
			gen_pk_file = None
			passwd = vm['passwd']
			if 'new_passwd' in vm and vm['new_passwd'] and change_pass_ok:
				passwd = vm['new_passwd']
	
	t = AnsibleThread(playbook_file, None, threads, gen_pk_file, passwd, retries, inventory_file, None, {'IM_HOST': vm['ip'] + ":" + str(vm['ssh_port'])})
	t.start()
	return t

def changeVMCredentials(vm):
	# Check if we must change user credentials in the VM
	if 'new_passwd' in vm and vm['new_passwd']:
		logger.info("Changing password to VM: " + vm['ip'])
		ssh_client = SSH(vm['ip'], vm['user'], vm['passwd'], vm['private_key'], vm['ssh_port'])
		(out, err, code) = ssh_client.execute('sudo bash -c \'echo "' + vm['user'] + ':' + vm['new_passwd'] + '" | /usr/sbin/chpasswd && echo "OK"\' 2> /dev/null')
		
		if code == 0:
			vm['passwd'] = vm['new_passwd']
			return True
		else:
			logger.error("Error changing password to VM: " + vm['ip'] + ". " + out + err)
			return False

	if 'new_public_key' in vm and vm['new_public_key'] and 'new_private_key' in vm and vm['new_private_key']:
		logger.info("Changing public key to VM: " + vm['ip'])
		ssh_client = SSH(vm['ip'], vm['user'], vm['passwd'], vm['private_key'], vm['ssh_port'])
		(out, err, code) = ssh_client.execute('echo ' + vm['new_public_key'] + ' >> .ssh/authorized_keys')
		if code != 0:
			logger.error("Error changing public key to VM:: " + vm['ip'] + ". " + out + err)
			return False
		else:
			vm['private_key'] = vm['new_private_key']
			return True

	return False

def removeRequiretty(vm):
	if not vm['master']:
		logger.info("Removing requiretty to VM: " + vm['ip'])
		ssh_client = SSH(vm['ip'], vm['user'], vm['passwd'], vm['private_key'], vm['ssh_port'])
		# Activate tty mode to avoid some problems with sudo in REL
		ssh_client.tty = True
		(stdout, stderr, code) = ssh_client.execute("sudo sed -i 's/.*requiretty$/#Defaults requiretty/' /etc/sudoers")
		logger.debug("OUT: " + stdout + stderr)
		return code == 0
	else:
		return True

def contextualize_vm(general_conf_data, vm_conf_data):
	res_data = {}
	pk_file = "/tmp/ansible_key"
	logger.info('Generate and copy the ssh key')
	
	# If the file exists, do not create it again
	if not os.path.isfile(pk_file):
		out = run_command('ssh-keygen -t rsa -C ' + getpass.getuser() + ' -q -N "" -f ' + pk_file)
		logger.debug(out)

	# Check that we can SSH access the node
	ctxt_vm = None
	for vm in general_conf_data['vms']:
		if vm['id'] == vm_conf_data['id']:
			ctxt_vm = vm
	
	if not ctxt_vm:
		logger.error("No VM to Contextualize!")
		res_data['OK'] = False
		return res_data
		
	for task in vm_conf_data['tasks']:
		logger.debug('Launch task: ' + task)
		playbook = general_conf_data['conf_dir'] + "/" + task + "_task_all.yml"
		inventory_file  = general_conf_data['conf_dir'] + "/hosts"
		
		if task == "basic":
			# This is always the fist step, so put the SSH test, the requiretty removal and change password here
			for vm in general_conf_data['vms']:
				logger.info("Waiting SSH access to VM: " + vm['ip'])
				if not wait_ssh_access(vm):
					logger.error("Error Waiting SSH access to VM: " + vm['ip'])
					res_data['SSH_WAIT'] = False
					res_data['OK'] = False
					return res_data
				else:
					res_data['SSH_WAIT'] = True
					logger.info("SSH access to VM: " + vm['ip']+ " Open!")
			
			# First remove requiretty in the node
			success = removeRequiretty(ctxt_vm)
			if success:
				logger.info("Requiretty successfully removed")
			else:
				logger.error("Error removing Requiretty")
			# Check if we must chage user credentials
			# Do not change it on the master. It must be changed only by the ConfManager
			change_creds = False
			if not ctxt_vm['master']:
				change_creds = changeVMCredentials(ctxt_vm)
				res_data['CHANGE_CREDS'] = change_creds
			
			# The basic task uses the credentials of VM stored in ctxt_vm
			ansible_thread = LaunchAnsiblePlaybook(playbook, ctxt_vm, 2, inventory_file, None, PLAYBOOK_RETRIES, change_creds)
		else:
			# in the other tasks pk_file can be used
			ansible_thread = LaunchAnsiblePlaybook(playbook, ctxt_vm, 2, inventory_file, pk_file, PLAYBOOK_RETRIES, True)
		
		(success, _) = wait_thread(ansible_thread)
		res_data[task] = success
		if not success:
			res_data['OK'] = False
			return res_data

	res_data['OK'] = True

	logger.info('Process finished')
	return res_data

if __name__ == "__main__":
	parser = OptionParser(usage="%prog [general_input_file] [vm_input_file]", version="%prog 1.0")
	(options, args) = parser.parse_args()
	
	if len(args) != 2:
		parser.error("Error: Incorrect parameters")
	
	# load json conf data
	general_conf_data = json.load(open(args[0]))
	vm_conf_data = json.load(open(args[1]))
	
	# Root logger: is used by paramiko
	logging.basicConfig(filename=vm_conf_data['remote_dir'] +"/ctxt_agent.log",
			    level=logging.WARNING,
			    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
			    datefmt='%m-%d-%Y %H:%M:%S')
	# ctxt_agent logger
	logger = logging.getLogger('ctxt_agent')
	logger.setLevel(logging.DEBUG)

	MAX_SSH_WAIT = 60

	if 'playbook_retries' in general_conf_data:
		PLAYBOOK_RETRIES = general_conf_data['playbook_retries']

	success = False
	res_data = contextualize_vm(general_conf_data, vm_conf_data)
	
	ctxt_out = open(vm_conf_data['remote_dir'] +"/ctxt_agent.out", 'w')
	json.dump(res_data, ctxt_out, indent=2)
	ctxt_out.close()

	if res_data['OK']:
		sys.exit(0)
	else:
		sys.exit(1)
