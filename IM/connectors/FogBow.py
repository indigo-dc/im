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

import json
import os
import requests
import time
from uuid import uuid1
from netaddr import IPNetwork, IPAddress

from IM.config import Config
from IM.uriparse import uriparse
from IM.VirtualMachine import VirtualMachine
from .CloudConnector import CloudConnector
from radl.radl import Feature


class FogBowCloudConnector(CloudConnector):
    """
    Cloud Launcher to the FogBow platform
    """

    type = "FogBow"
    """str with the name of the provider."""

    VM_STATE_MAP = {
        'INACTIVE': VirtualMachine.STOPPED,
        'CREATING': VirtualMachine.PENDING,
        'ATTACHING': VirtualMachine.PENDING,
        'DISPATCHED': VirtualMachine.PENDING,
        'SPAWNING': VirtualMachine.PENDING,
        'READY': VirtualMachine.RUNNING,
        'IN_USE': VirtualMachine.RUNNING,
        'FAILED': VirtualMachine.FAILED,
        'INCONSISTENT': VirtualMachine.UNKNOWN,
        'UNAVAILABLE': VirtualMachine.STOPPED
    }
    """Dictionary with a map with the FogBow VM states to the IM states."""

    MAX_ADD_IP_COUNT = 5
    """ Max number of retries to get a public IP """

    def __init__(self, cloud_info, inf):
        self.add_public_ip_count = 0
        self.token = None
        CloudConnector.__init__(self, cloud_info, inf)

    def get_full_url(self, url):
        protocol = "http"
        if self.cloud.protocol:
            protocol = self.cloud.protocol

        if self.cloud.port > 0:
            url = "%s://%s:%d%s%s" % (protocol, self.cloud.server, self.cloud.port, self.cloud.path, url)
        else:
            url = "%s://%s%s%s" % (protocol, self.cloud.server, self.cloud.path, url)
        return url

    def create_request(self, method, url, auth_data, headers=None, body=None):
        auth_header = self.get_auth_header(auth_data)
        if auth_header:
            if headers is None:
                headers = {}
            headers.update(auth_header)

        resp = requests.request(method, self.get_full_url(url), verify=self.verify_ssl, headers=headers, data=body)

        return resp

    def post_and_get(self, path, body, auth_data):
        headers = {'Content-Type': 'application/json'}
        resp = self.create_request('POST', path, auth_data, headers, body)
        if resp.status_code not in [201, 200]:
            self.log_error("Error creating %s. %s. %s." % (path, resp.reason, resp.text))
            return None
        else:
            obj_id = resp.text
            resp = self.create_request('GET', '%s%s' % (path, obj_id), auth_data, headers)
            if resp.status_code == 200:
                obj_info = resp.json()
                if obj_info['state'] == 'FAILED':
                    self.log_error("%s%s is FAILED." % (path, obj_id))
                    try:
                        resp = self.create_request('DELETE', '%s%s' % (path, obj_id), auth_data, headers)
                        if resp.status_code not in [200, 204]:
                            self.log_error("Error deleting %s%s." % (path, obj_id))
                        else:
                            self.log_info("%s%s deleted." % (path, obj_id))
                    except:
                        self.log_exception("Error deleting %s%s." % (path, obj_id))
                else:
                    return obj_info
            else:
                self.log_error("Error %s%s. %s. %s." % (path, obj_id, resp.reason, resp.text))

        return None

    def get_token(self, auth_data):
        headers = {'Content-Type': 'application/json'}

        if self.token:
            self.log_debug("We have a token. Check if it is valid.")
            resp = requests.request('HEAD', self.get_full_url('/images/'), verify=self.verify_ssl)
            if resp.status_code in [200, 201]:
                return self.token
            else:
                self.log_debug("It is not valid. Request for a new one.")
                self.token = None

        body = {}
        for key, value in auth_data.items():
            if key not in ['id', 'type', 'host']:
                body[key] = value
        resp = requests.request('POST', self.get_full_url('/tokens/'), verify=self.verify_ssl,
                                headers=headers, data=json.dumps(body))
        if resp.status_code in [200, 201]:
            self.token = resp.text
            return resp.text
        else:
            self.log_error("Error getting token: %s. %s" % (resp.reason, resp.text))
            raise Exception("Error getting token: %s. %s" % (resp.reason, resp.text))

    def get_auth_header(self, auth_data):
        """
        Generate the auth header needed to contact with the FogBow server.
        """
        auth = auth_data.getAuthInfo(FogBowCloudConnector.type)
        if not auth:
            raise Exception("No correct auth data has been specified to FogBow.")

        if 'token' in auth[0]:
            token = auth[0]['token']
        else:
            token = self.get_token(auth[0])

        auth_headers = {'federationTokenValue': token}

        return auth_headers

    def concreteSystem(self, radl_system, auth_data):
        image_urls = radl_system.getValue("disk.0.image.url")
        if not image_urls:
            return [radl_system.clone()]
        else:
            if not isinstance(image_urls, list):
                image_urls = [image_urls]

            res = []
            for str_url in image_urls:
                url = uriparse(str_url)
                protocol = url[0]
                src_host = url[1].split(':')[0]
                # TODO: check the port
                if protocol == "fbw" and self.cloud.server == src_host:
                    res_system = radl_system.clone()

                    res_system.addFeature(
                        Feature("disk.0.image.url", "=", str_url), conflict="other", missing="other")

                    res_system.addFeature(
                        Feature("provider.type", "=", self.type), conflict="other", missing="other")
                    res_system.addFeature(Feature(
                        "provider.host", "=", self.cloud.server), conflict="other", missing="other")
                    if self.cloud.port != -1:
                        res_system.addFeature(Feature(
                            "provider.port", "=", self.cloud.port), conflict="other", missing="other")

                    res_system.delValue('disk.0.os.credentials.username')
                    res_system.setValue('disk.0.os.credentials.username', 'fogbow')

                    res.append(res_system)

            return res

    def get_fbw_nets(self, auth_data):
        """
        Get a dict with the name and ID of the fogbow nets
        """
        fbw_nets = {}
        resp = self.create_request('GET', '/networks/status', auth_data)
        if resp.status_code == 200:
            for net in resp.json():
                fbw_nets[net['instanceName']] = net['instanceId']
        else:
            raise Exception("Error getting networks: %s. %s" % (resp.reason, resp.text))
        return fbw_nets

    def create_nets(self, inf, radl, auth_data):
        fbw_nets = self.get_fbw_nets(auth_data)

        nets = {}
        for net in radl.networks:
            if not net.isPublic():
                net_name = "im_%s_%s" % (inf.id, net.id)

                if net_name in fbw_nets:
                    self.log_info("Net %s exists in FogBow do not create it again." % net_name)
                else:
                    self.log_info("Creating net %s." % net_name)

                    body = {"allocationMode": "dynamic", "name": net_name}

                    net_info = self.post_and_get('/networks/', json.dumps(body), auth_data)
                    if net_info:
                        net.setValue("provider_id", net_info['id'])
                    else:
                        self.log_error("Error creating net %s." % net_name)

        return nets

    def launch(self, inf, radl, requested_radl, num_vm, auth_data):
        system = radl.systems[0]
        res = []
        i = 0

        image = os.path.basename(system.getValue("disk.0.image.url"))

        # set the credentials the FogBow default username: fogbow
        system.delValue('disk.0.os.credentials.username')
        system.setValue('disk.0.os.credentials.username', 'fogbow')

        public_key = system.getValue('disk.0.os.credentials.public_key')

        if not public_key:
            # We must generate them
            (public_key, private_key) = self.keygen()
            system.setValue('disk.0.os.credentials.private_key', private_key)

        cpu = system.getValue('cpu.count')
        memory = system.getFeature('memory.size').getValue('M')
        name = system.getValue("instance_name")
        if not name:
            name = system.getValue("disk.0.image.name")
        if not name:
            name = "userimage"

        with inf._lock:
            self.create_nets(inf, radl, auth_data)

        while i < num_vm:
            try:
                headers = {'Content-Type': 'application/json'}

                nets = []
                for net in radl.networks:
                    if not net.isPublic() and radl.systems[0].getNumNetworkWithConnection(net.id) is not None:
                        provider_id = net.getValue('provider_id')
                        if provider_id:
                            nets.append(provider_id)

                body = {"computeOrder":
                        {"imageId": image,
                         "memory": memory,
                         "name": "%s-%s" % (name.lower().replace("_", "-"), str(uuid1())),
                         "publicKey": public_key,
                         "vCPU": cpu}
                        }

                if nets:
                    body["networkIds"] = nets

                if system.getValue('availability_zone'):
                    body['provider'] = system.getValue('availability_zone')

                resp = self.create_request('POST', '/computes/', auth_data, headers, json.dumps(body))

                if resp.status_code not in [201, 200]:
                    res.append((False, resp.reason + "\n" + resp.text))
                else:
                    vm = VirtualMachine(inf, str(resp.text), self.cloud, radl, requested_radl)
                    vm.info.systems[0].setValue('instance_id', str(vm.id))
                    inf.add_vm(vm)
                    res.append((True, vm))

            except Exception as ex:
                self.log_exception("Error connecting with FogBow manager")
                res.append((False, "ERROR: " + str(ex)))

            i += 1

        return res

    def wait_volume(self, volume_id, auth_data, state='READY', timeout=60, delay=5):
        """
        Wait a volume to be in certain state.
        """
        if volume_id:
            count = 0
            vol_state = ""
            while vol_state != state and vol_state != "FAILED" and count < timeout:
                time.sleep(delay)
                count += delay
                resp = self.create_request('GET', '/volumes/%s' % volume_id, auth_data)
                if resp.status_code != 200:
                    self.log_error("Error getting volume state: %s. %s." % (resp.reason, resp.text))
                    return False
                else:
                    vol_state = resp.json()["state"]

            return vol_state == state
        else:
            return False

    def attach_volumes(self, vm, auth_data):
        """
        Attach a the required volumes (in the RADL) to the launched node

        Arguments:
           - vm(:py:class:`IM.VirtualMachine`): VM information.
           - node(:py:class:`libcloud.compute.base.Node`): node object.
        """
        try:
            headers = {'Content-Type': 'application/json'}
            if "volumes" not in vm.__dict__.keys():
                vm.volumes = []
                cont = 1
                while (vm.info.systems[0].getValue("disk." + str(cont) + ".size") or
                       vm.info.systems[0].getValue("disk." + str(cont) + ".image.url")):
                    disk_size = None
                    if vm.info.systems[0].getValue("disk." + str(cont) + ".size"):
                        disk_size = vm.info.systems[0].getFeature("disk." + str(cont) + ".size").getValue('G')
                    disk_device = vm.info.systems[0].getValue("disk." + str(cont) + ".device")
                    disk_url = vm.info.systems[0].getValue("disk." + str(cont) + ".image.url")
                    if disk_device:
                        disk_device = "/dev/" + disk_device
                    else:
                        disk_device = "/dev/hdb"
                    if disk_url:
                        volume_id = os.path.basename(disk_url)
                        try:
                            resp = self.create_request('GET', '/volumes/%s' % volume_id, auth_data, headers)
                            resp.raise_for_status()
                            success = True
                        except:
                            success = False
                            self.log_exception("Error getting volume ID %s" % volume_id)
                    else:
                        self.log_debug("Creating a %d GB volume for the disk %d" % (int(disk_size), cont))
                        volume_name = "im-%s" % str(uuid1())

                        body = '{"name": "%s", "volumeSize": %d}' % (volume_name, int(disk_size))
                        resp = self.create_request('POST', '/volumes/', auth_data, headers, body)

                        if resp.status_code not in [201, 200]:
                            self.log_error("Error creating volume: %s. %s" % (resp.reason, resp.text))
                        else:
                            volume_id = resp.text

                        success = self.wait_volume(volume_id, auth_data)
                        if success:
                            # Add the volume to the VM to remove it later
                            vm.volumes.append(volume_id)

                    if success:
                        self.log_debug("Attach the volume ID %s" % volume_id)
                        body = '{"computeId": "%s","device": "%s","volumeId": "%s"}' % (vm.id, disk_device, volume_id)
                        attach_info = self.post_and_get('/attachments/', body, auth_data)
                        if attach_info:
                            disk_device = attach_info["device"]
                            if disk_device:
                                vm.info.systems[0].setValue("disk." + str(cont) + ".device", disk_device)
                        else:
                            success = False

                    if not success:
                        self.log_error("Error waiting the volume ID not attaching to the VM.")
                        if not disk_url:
                            self.log_error("Destroying it.")
                            resp = self.create_request('DELETE', '/volumes/%s' % volume_id, auth_data, headers)
                            if resp.status_code not in [204, 200, 404]:
                                self.log_error("Error deleting volume: %s. %s" % (resp.reason, resp.text))

                    cont += 1
            return True
        except Exception:
            self.log_exception("Error creating or attaching the volume to the node")
            return False

    def _get_instance_public_ips(self, vm_id, auth_data, field="ip"):
        """
        Get the IPs associated with the compute specified
        """
        res = []
        try:
            headers = {'Accept': 'application/json'}
            resp = self.create_request('GET', '/publicIps/status', auth_data, headers=headers)
            if resp.status_code == 200:
                for ipstatus in resp.json():
                    resp_ip = self.create_request('GET', '/publicIps/%s' % ipstatus['instanceId'], auth_data, headers)
                    if resp_ip.status_code == 200:
                        ipdata = resp_ip.json()
                        if ipdata['state'] == 'FAILED':
                            try:
                                self.log_warn("Public IP id: %s is FAILED. Trying to delete." % ipstatus['instanceId'])
                                resp_del = self.create_request('DELETE', '/publicIps/%s' % ipstatus['instanceId'],
                                                               auth_data, headers)
                                if resp_del.status_code in [200, 204]:
                                    self.log_info("Public IP id: %s deleted." % ipstatus['instanceId'])
                                else:
                                    self.log_warn("Error deleting public IP id: %s. %s. %s." % (ipstatus['instanceId'],
                                                                                                resp.reason, resp.text))
                            except:
                                self.log_warn("Error deleting public IP id: %s" % ipstatus['instanceId'])

                        elif ipdata['computeId'] == vm_id:
                            res.append(ipdata[field])
                    else:
                        self.log_error("Error getting public IP info: %s. %s." % (resp.reason, resp.text))
            else:
                self.log_error("Error getting public IP info: %s. %s." % (resp.reason, resp.text))
        except:
            self.log_exception("Error getting public IP info")
        return res

    def add_elastic_ip(self, vm, public_ips, auth_data):
        """
        Get a public IP if needed.
        """
        if self.add_public_ip_count >= self.MAX_ADD_IP_COUNT:
            self.log_error("Error adding a floating IP: Max number of retries reached.")
            self.error_messages += "Error adding a floating IP: Max number of retries reached.\n"
            return None

        if not public_ips and vm.hasPublicNet() and vm.state == VirtualMachine.RUNNING:
            self.log_debug("VM ID %s requests a public IP and it does not have it. Requesting the IP." % vm.id)
            body = '{"computeId": "%s"}' % vm.id

            ip_info = self.post_and_get('/publicIps/', body, auth_data)
            if ip_info:
                self.log_debug("IP obtained: %s." % ip_info['ip'])
                return ip_info['ip']
            else:
                self.add_public_ip_count += 1
                self.log_warn("Error adding a floating IP the VM: (%d/%d)\n" % (self.add_public_ip_count,
                                                                                self.MAX_ADD_IP_COUNT))
                self.error_messages += "Error adding a floating IP: (%d/%d)\n" % (self.add_public_ip_count,
                                                                                  self.MAX_ADD_IP_COUNT)
                return None

    def updateVMInfo(self, vm, auth_data):
        try:
            # First get the request info
            headers = {'Accept': 'application/json'}
            resp = self.create_request('GET', "/computes/" + vm.id, auth_data, headers=headers)

            if resp.status_code != 200:
                return (False, resp.reason + "\n" + resp.text)
            else:
                output = resp.json()
                vm.state = self.VM_STATE_MAP.get(output["state"], VirtualMachine.UNKNOWN)

                if "vCPU" in output and output["vCPU"]:
                    vm.info.systems[0].addFeature(Feature(
                        "cpu.count", "=", output["vCPU"]), conflict="other", missing="other")
                if "memory" in output and output["memory"]:
                    vm.info.systems[0].addFeature(Feature(
                        "memory.size", "=", output["memory"], 'M'), conflict="other", missing="other")
                if "disk" in output and output["disk"]:
                    vm.info.systems[0].addFeature(Feature(
                        "disk.0.size", "=", output["disk"], 'G'), conflict="other", missing="other")

                # Update the network data
                private_ips = []
                public_ips = []
                if "ipAddresses" in output and output["ipAddresses"]:
                    for ip in output["ipAddresses"]:
                        is_public = not (any([IPAddress(ip) in IPNetwork(mask)
                                              for mask in Config.PRIVATE_NET_MASKS]))
                        if is_public:
                            public_ips.append(ip)
                        else:
                            private_ips.append(ip)

                ip = self.add_elastic_ip(vm, public_ips, auth_data)
                if ip:
                    public_ips.append(ip)
                vm.setIps(public_ips, private_ips)

                self.attach_volumes(vm, auth_data)

                return (True, vm)
        except Exception as ex:
            self.log_exception("Error connecting with FogBow Manager")
            return (False, "Error connecting with FogBow Manager: %s" % ex.message)

    def finalize(self, vm, last, auth_data):
        if not vm.id:
            self.log_warn("No VM ID. Ignoring")
            return True, "No VM ID. Ignoring"

        headers = {'Accept': 'text/plain'}

        public_ips = self._get_instance_public_ips(vm.id, auth_data, "id")

        try:
            resp = self.create_request('DELETE', "/computes/" + vm.id, auth_data, headers=headers)

            if resp.status_code == 404:
                vm.state = VirtualMachine.OFF
                res = (True, "")
            elif resp.status_code not in [200, 204]:
                res = (False, "Error removing the VM: " + resp.reason + "\n" + resp.text)
            else:
                res = (True, "")

            retries = 3
            success = False
            cont = 0
            while not success and cont < retries:
                cont += 1
                success = self.delete_volumes(vm, auth_data)

            success = False
            cont = 0
            while not success and cont < retries:
                cont += 1
                success = self.delete_public_ips(vm.id, public_ips, auth_data)

            if last:
                success = False
                cont = 0
                while not success and cont < retries:
                    cont += 1
                    success = self.delete_nets(vm, auth_data)

            return res
        except Exception as ex:
            self.log_exception("Error connecting with FogBow server")
            return (False, "Error connecting with FogBow server: %s" % ex.message)

    def delete_nets(self, vm, auth_data):
        """
        Delete the created nets
        """
        try:
            fbw_nets = self.get_fbw_nets(auth_data)
        except:
            self.log_exception("Error getting FogBow nets.")
            fbw_nets = {}
        success = True
        try:
            for net in vm.info.networks:
                if not net.isPublic():
                    net_name = "im_%s_%s" % (vm.inf.id, net.id)
                    if net_name in fbw_nets:
                        net_id = fbw_nets[net_name]
                        resp = self.create_request('DELETE', '/networks/%s' % net_id, auth_data)
                        if resp.status_code not in [200, 204, 404]:
                            success = False
                            self.log_error("Error deleting net %s: %s. %s." % (net_name, resp.reason, resp.text))
                        else:
                            self.log_info("Net %s: Successfully deleted." % net_name)
        except:
            success = False
            self.log_exception("Error deleting net %s." % net_name)
        return success

    def delete_volumes(self, vm, auth_data):
        """
        Delete the volumes of a VM
        """
        all_ok = True
        if "volumes" in vm.__dict__.keys() and vm.volumes:
            for volumeid in vm.volumes:
                self.log_debug("Deleting volume ID %s" % volumeid)
                try:
                    resp = self.create_request('DELETE', '/volumes/%s' % volumeid, auth_data)
                    if resp.status_code not in [200, 204, 404]:
                        success = False
                        raise Exception(resp.reason + "\n" + resp.text)
                    else:
                        success = True
                except:
                    self.log_exception("Error destroying the volume: " + str(volumeid) +
                                       " from the node: " + str(vm.id))
                    success = False

                if not success:
                    all_ok = False
        return all_ok

    def delete_public_ips(self, vm_id, public_ips, auth_data):
        """
        Release the public IPs of this VM
        """
        all_ok = True
        for ip_id in public_ips:
            try:
                self.log_info("Deleting IP with ID: %s" % ip_id)
                resp = self.create_request('DELETE', '/publicIps/%s' % ip_id, auth_data)
                if resp.status_code not in [200, 204, 404]:
                    success = False
                    raise Exception(resp.reason + "\n" + resp.text)
                success = True
            except:
                self.log_exception("Error releasing the IP: " + str(ip_id) +
                                   " from the node: " + str(vm_id))
                success = False
            if not success:
                all_ok = False
        return all_ok

    def stop(self, vm, auth_data):
        return (False, "Not supported")

    def start(self, vm, auth_data):
        return (False, "Not supported")

    def alterVM(self, vm, radl, auth_data):
        return (False, "Not supported")
