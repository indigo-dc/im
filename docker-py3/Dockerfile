# Dockerfile to create a container with the IM service
FROM ubuntu:18.04
LABEL maintainer="Miguel Caballer <micafer1@upv.es>"
LABEL version="1.9.2"
LABEL description="Container image to run the IM service. (http://www.grycap.upv.es/im)"
EXPOSE 8899 8800
ARG BRANCH=master

# Ensure system is up to date with mandatory python packages installed
RUN apt-get update && apt-get install --no-install-recommends -y python3 openssh-client sshpass vim libmysqlclient20 python3-mysqldb && \
     apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* && rm -rf ~/.cache/

# Install IM
RUN apt-get update && apt-get install --no-install-recommends -y git python3-pip && \
     pip3 install pip setuptools --upgrade && \
     /usr/local/bin/pip3 install msrest msrestazure azure-common azure-mgmt-storage azure-mgmt-compute azure-mgmt-network azure-mgmt-resource azure-mgmt-dns azure-storage && \
     /usr/local/bin/pip3 install pyOpenSSL cheroot xmltodict pymongo ansible==2.7.13 && \
     # Install github version until the v 3.0 is released
     cd /tmp && git clone https://github.com/apache/libcloud && /usr/local/bin/pip3 install /tmp/libcloud && \
     cd /tmp && git clone https://github.com/indigo-dc/im --branch $BRANCH && /usr/local/bin/pip3 install /tmp/im && \
     /usr/local/bin/pip3 uninstall pip -y && \
     apt-get purge -y git python3-pip && \
     apt-get autoremove -y && apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* && rm -rf ~/.cache/

# Copy a ansible.cfg with correct minimum values
COPY ansible.cfg /etc/ansible/ansible.cfg

# Start IM service
CMD im_service.py
