#!/usr/bin/env python
'''
ko - Kubernetes Openstack

Author: Rich Wellum (richwellum@gmail.com)

This tool provides a method to deploy OpenStack on a Kubernetes Cluster using
Kolla and Kolla-Kubernetes on bare metal servers or virtual machines. Virtual
machines supported are Ubuntu and Centos.

Host machine requirements
=========================

The host machine must satisfy the following minimum requirements:

- 2 network interfaces
- 16GB main memory
- 80GB disk space

Root access to the deployment host machine is required.

Prerequisites
=============

Verify the state of network interfaces. If using a VM spawned on
OpenStack as the host machine, the state of the second interface will be DOWN
on booting the VM.

    ip addr show

Bring up the second network interface if it is down.

    ip link set ens4 up

Verify if the second interface has an IP address.

    ip addr show

Preceding
=========

This relies heavily on the OpenStack kolla-kubernetes project and in
particular the Bare Metal Deployment Guide:

https://docs.openstack.org/developer/kolla-kubernetes/deployment-guide.html

However support will be added to also install OpenStack with the openstack-helm
project.

Purpose
=======

The purpose of this tool, when there are so many others out there is:

1. Many tools don't support both Centos and Ubuntu with no input from the user.
2. I like to play with versions of all the supporting tools, it helps when users
report issues when they upgrade say helm, or docker, or kubernetes.
3. This tool verifies it's completeness by generating a VM in the OpenStack
Cluster.
4. Contains a demo mode that walks the user through Kubernetes and OpenStack

Mandatory Inputs
================

1. mgmt_int (network_interface):
Name of the interface to be used for management operations
The `network_interface` variable is the interface to which Kolla binds API
services. For example, when starting Mariadb, it will bind to the IP on the
interface list in the ``network_interface`` variable.

2. mgmt_ip    : IP Address of management interface (mgmt_int)

3. neutron_int (neutron_external_interface):
Name of the interface to be used for Neutron operations
The `neutron_external_interface` variable is the interface that will be used
for the external bridge in Neutron. Without this bridge the deployment instance
traffic will be unable to access the rest of the Internet.

4. keepalived:
An unused IP address in the network to act as a VIP for
`kolla_internal_vip_address`. The VIP will be used with keepalived and added
to the ``api_interface`` as specified in the ``globals.yml``


TODO
====

1. Make it work on a baremetal host
2. Potentially build a docker container or VM to run this on
5. Add option to use a CNI other than canal
6. Make it work with os-helm
7. Verify networks - as per kolla/kolla-ansible/doc/quickstart.rst
8. Add steps to output (1/17 etc)

Dependencies
============
'''

from __future__ import print_function
import sys
import os
import time
import subprocess
import argparse
from argparse import RawDescriptionHelpFormatter
import logging
import platform
import re
import tarfile

__author__ = 'Rich Wellum'
__copyright__ = 'Copyright 2017, Rich Wellum'
__license__ = ''
__version__ = '1.0.0'
__maintainer__ = 'Rich Wellum'
__email__ = 'rwellum@gmail.com'

TIMEOUT = 600

logger = logging.getLogger(__name__)


def set_logging():
    '''
    Set basic logging format.
    '''
    FORMAT = "[%(asctime)s.%(msecs)03d %(levelname)8s: %(funcName)20s:%(lineno)s] %(message)s"
    logging.basicConfig(format=FORMAT, datefmt="%H:%M:%S")


class AbortScriptException(Exception):
    '''Abort the script and clean up before exiting.'''


def parse_args():
    '''Parse sys.argv and return args'''
    parser = argparse.ArgumentParser(
        formatter_class=RawDescriptionHelpFormatter,
        description='This tool provides a method to deploy OpenStack on a ' +
        'Kubernetes Cluster using Kolla and Kolla-Kubernetes on bare metal ' +
        'servers or virtual machines. Virtual machines supported are Ubuntu and ' +
        'Centos.\n' +
        'The host machine must satisfy the following minimum requirements:\n' +
        '- 2 network interfaces\n' +
        '- 16GB main memory\n' +
        '- 80GB disk space\n' +
        'Root access to the deployment host machine is required.',
        epilog='E.g.: k8s.py eth0 10.240.43.250 eth1 10.240.43.251 -v -kv 1.6.2 -hv 2.4.2\n')
    parser.add_argument('MGMT_INT',
                        help='The interface to which Kolla binds API services, E.g: eth0')
    parser.add_argument('MGMT_IP',
                        help='MGMT_INT IP Address, E.g: 10.240.83.111')
    parser.add_argument('NEUTRON_INT',
                        help='The interface that will be used for the external ' +
                        'bridge in Neutron, E.g: eth1')
    parser.add_argument('VIP_IP',
                        help='Keepalived VIP, used with keepalived should be ' +
                        'an unused IP on management NIC subnet, E.g: 10.240.83.112')
    parser.add_argument('-it', '--image_tag', type=str, default='4.0.0',
                        help='Specify a different Kolla image tage to the default(4.0.0)')
    parser.add_argument('-lv', '--latest_version', action='store_true',
                        help='Try to install all the latest versions of tools')
    parser.add_argument('-hv', '--helm_version', type=str, default='2.5.0',
                        help='Specify a different helm version to the default(2.5.0)')
    parser.add_argument('-kv', '--k8s_version', type=str, default='1.6.5',
                        help='Specify a different ansible version to the default(1.6.5)')
    parser.add_argument('-av', '--ansible_version', type=str, default='2.2.0.0',
                        help='Specify a different k8s version to the default(2.2.0.0)')
    parser.add_argument('-jv', '--jinja2_version', type=str, default='2.8.1',
                        help='Specify a different jinja2 version to the default(2.8.1)')
    parser.add_argument('-cv', '--cni_version', type=str, default='0.5.1-00',
                        help='Specify a different kubernetes-cni version to the default(0.5.1-00)')
    parser.add_argument('-c', '--cleanup', action='store_true',
                        help='YMMV: Cleanup existing Kubernetes cluster before ' +
                        'creating a new one')
    parser.add_argument('-cc', '--complete_cleanup', action='store_true',
                        help='Cleanup existing Kubernetes cluster then exit, ' +
                        'reboot host is advised')
    parser.add_argument('-k8s', '--kubernetes', action='store_true',
                        help='Stop after bringing up kubernetes, do not install OpenStack')
    # Todo: make this the default then add a switch for kolla or os-helm
    parser.add_argument('-os', '--openstack', action='store_true',
                        help='Build OpenStack on an existing Kubernetes Cluster')
    parser.add_argument('-n', '--nslookup', action='store_true',
                        help='Pause for the user to manually test nslookup in kubernetes cluster')
    # parser.add_argument('-l,', '--cloud', type=int, default=3,
    #                     help='optionally change cloud network config files from default(3)')
    parser.add_argument('-v', '--verbose', action='store_const',
                        const=logging.DEBUG, default=logging.INFO,
                        help='Turn on verbose messages')
    parser.add_argument('-d', '--demo', action='store_true',
                        help='Display some demo information and offer to move on')
    parser.add_argument('-f', '--force', action='store_true',
                        help='When used in conjunction with --demo - it will proceed without user input')

    return parser.parse_args()


def run_shell(cmd):
    '''Run a shell command and return the output
    Print the output if debug is enabled
    Not using logger.debug as a bit noisy for this info'''
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    out = p.stdout.read()
    if DEMO:
        if not re.search('kubectl get pods', cmd):
            print('DEMO: CMD: "%s"' % cmd)

    if DEBUG == 10:  # Hack - debug enabled
        if out:
            print('Shell output: %s' % out)
    return(out)


def untar(fname):
    '''Untar a tarred and compressed file'''
    if (fname.endswith("tar.gz")):
        tar = tarfile.open(fname, "r:gz")
        tar.extractall()
        tar.close()
    elif (fname.endswith("tar")):
        tar = tarfile.open(fname, "r:")
        tar.extractall()
        tar.close()


def pause_to_debug(str):
    '''Pause the script for manual debugging of the VM before continuing'''
    print('Pause: "%s"' % str)
    raw_input('Press Enter to continue\n')


def demo(title, description):
    '''Pause the script to provide demo information'''
    if not DEMO:
        return

    banner = len(description)
    if banner > 100:
        banner = 100

    # First banner
    print('\n')
    for c in range(banner):
        print('*', end='')

    # Add DEMO string
    print('\n%s'.ljust(banner - len('DEMO')) % 'DEMO')

    # Add title formatted to banner length
    print('%s'.ljust(banner - len(title)) % title)

    # Add description
    print('%s' % description)

    # Final banner
    for c in range(banner):
        print('*', end='')
    print('\n')

    if not FORCE:
        raw_input('Press Enter to continue with demo...')
    else:
        print('Demo: Continuing with Demo')


def curl(*args):
    '''Use curl to retrieve a file from a URI'''
    curl_path = '/usr/bin/curl'
    curl_list = [curl_path]
    for arg in args:
        curl_list.append(arg)
    curl_result = subprocess.Popen(
        curl_list,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE).communicate()[0]
    return curl_result


def linux_ver():
    '''Determine Linux version - Ubuntu or Centos
    Fail if it is not one of those'''
    global LINUX

    find_os = platform.linux_distribution()
    if re.search('Centos', find_os[0], re.IGNORECASE):
        LINUX = 'Centos'
    elif re.search('Ubuntu', find_os[0], re.IGNORECASE):
        LINUX = 'Ubuntu'
    else:
        print('Linux "%s" is not supported yet' % find_os[0])
        sys.exit(1)

    return(str(find_os))


def docker_ver():
    '''Display docker version'''
    oldstr = run_shell("docker --version | awk '{print $3}'")
    newstr = oldstr.replace(",", "")
    # docker_ver2 = news_shell('echo `${%s:0:-4}`' % docker_ver)
    return(newstr.rstrip())


def print_versions(args):
    '''Print out versions of all the various tools needed'''
    print('\n%s - Networking:' % __file__)
    print('Management Int:  %s' % args.MGMT_INT)
    print('Management IP:   %s' % args.MGMT_IP)
    print('Neutron Int:     %s' % args.NEUTRON_INT)
    print('VIP Keepalive:   %s' % args.VIP_IP)

    print('\n%s - Versions:' % __file__)
    print('Docker version:  %s' % docker_ver())
    print('Helm version:    %s' % args.helm_version)
    print('K8s version:     %s' % args.k8s_version)
    # print('K8s CNI version: %s' % args.cni_version)
    print('Ansible version: %s' % args.ansible_version)
    print('Jinja2 version:  %s' % args.jinja2_version)
    print('Image Tag:       %s' % args.image_tag)
    print('Linux info:      %s\n' % linux_ver())
    time.sleep(1)


def k8s_create_repo():
    '''Create a k8s repository file'''
    if LINUX == 'Centos':
        name = './kubernetes.repo'
        repo = '/etc/yum.repos.d/kubernetes.repo'
        with open(name, "w") as w:
            w.write("""\
[kubernetes]
name=Kubernetes
baseurl=http://yum.kubernetes.io/repos/kubernetes-el7-x86_64
enabled=1
gpgcheck=0
repo_gpgcheck=1
gpgkey=https://packages.cloud.google.com/yum/doc/yum-key.gpg
       https://packages.cloud.google.com/yum/doc/rpm-package-key.gpg
""")
        # todo: add -H to all sudo's see ifit works in both envs
        run_shell('sudo mv ./kubernetes.repo %s' % repo)
    else:
        run_shell('curl -s https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo -E apt-key add -')
        name = './kubernetes.list'
        repo = '/etc/apt/sources.list.d/kubernetes.list'
        with open(name, "w") as w:
            w.write("""\
deb http://apt.kubernetes.io/ kubernetes-xenial main
""")
        run_shell('sudo mv ./kubernetes.list %s' % repo)
        run_shell('sudo apt-get update')


def k8s_wait_for_kube_system():
    '''Wait for basic k8s to come up'''

    TIMEOUT = 350  # Give k8s 350s to come up
    RETRY_INTERVAL = 10
    elapsed_time = 0

    print('Kubernetes - Wait for basic Kubernetes (6 pods) infrastructure')
    time.sleep(RETRY_INTERVAL)
    while True:
        pod_status = run_shell('kubectl get pods -n kube-system --no-headers')
        nlines = len(pod_status.splitlines())
        if nlines == 6:
            print('Kubernetes - All pods %s/6 are started, continuing' % nlines)
            run_shell('kubectl get pods -n kube-system')
            break
        elif elapsed_time < TIMEOUT:
            if nlines < 0:
                cnt = 0
            else:
                cnt = nlines

            if elapsed_time is not 0:
                print('Kubernetes - Pod status after %d seconds, pods up %s:6 - '
                      'sleep %d seconds and retry'
                      % (elapsed_time, cnt, RETRY_INTERVAL))
            time.sleep(RETRY_INTERVAL)
            elapsed_time = elapsed_time + RETRY_INTERVAL
            continue
        else:
            # Dump verbose output in case it helps...
            print(pod_status)
            raise AbortScriptException(
                "Kubernetes - did not come up after {0} seconds!"
                .format(elapsed_time))


def k8s_wait_for_running_negate():
    '''Query get pods until only state is Running'''

    TIMEOUT = 1000  # Give k8s 1000s to come up
    RETRY_INTERVAL = 5

    print("Kubernetes - Wait for all pods to be in Running state:")
    elapsed_time = 0
    prev_not_running = 0
    while True:
        etcd_check = run_shell('kubectl get pods --no-headers --all-namespaces \
        | grep -i "request timed out" | wc -l')

        if int(etcd_check) != 0:
            print('Kubernetes - etcdserver is busy - retrying after brief pause')
            time.sleep(15)
            continue

        not_running = run_shell(
            'kubectl get pods --no-headers --all-namespaces | grep -v "Running" | wc -l')

        if int(not_running) != 0:
            if prev_not_running != not_running:
                print('Kubernetes - %s pod(s) are not in Running state' % int(not_running))
            time.sleep(RETRY_INTERVAL)
            elapsed_time = elapsed_time + RETRY_INTERVAL
            prev_not_running = not_running
            continue
        else:
            print('Kubernetes - All pods are in Running state')
            time.sleep(5)
            break

        if elapsed_time > TIMEOUT:
            # Dump verbose output in case it helps...
            print(int(not_running))
            raise AbortScriptException(
                "Kubernetes did not come up after {0} 1econds!"
                .format(elapsed_time))
            sys.exit(1)


def k8s_wait_for_vm(vm):
    """Wait for a vm to be listed as running in nova list"""

    TIMEOUT = 100
    RETRY_INTERVAL = 5

    print("Kubernetes - Wait for VM %s to be in running state:" % vm)
    elapsed_time = 0

    while True:
        nova_out = run_shell(
            '.  ~/keystonerc_admin; nova list | grep %s' % vm)
        if not re.search('Running', nova_out):
            print('Kubernetes - VM %s is not Running yet' % vm)
            time.sleep(RETRY_INTERVAL)
            elapsed_time = elapsed_time + RETRY_INTERVAL
            continue
        else:
            print('Kubernetes - VM %s is Running' % vm)
            break

        if elapsed_time > TIMEOUT:
            # Dump verbose output in case it helps...
            print(nova_out)
            raise AbortScriptException(
                "VM did not come up after {0} 1econds!"
                .format(elapsed_time))
            sys.exit(1)


def k8s_install_tools(a_ver, j_ver):
    '''Basic tools needed for first pass'''
    print('Kolla - Install necessary tools')

    if LINUX == 'Centos':
        run_shell('sudo yum update -y; sudo yum upgrade -y')
        run_shell('sudo yum install -y epel-release bridge-utils nmap')
        run_shell('sudo yum install -y python-pip python-devel libffi-devel \
        gcc openssl-devel sshpass')
        run_shell('sudo yum install -y git crudini jq ansible')
    else:
        run_shell('sudo apt-get update -y; sudo apt-get upgrade -y --allow-downgrades')
        run_shell('sudo apt-get install -y bridge-utils nmap')
        run_shell('sudo apt-get install -y python-dev libffi-dev gcc libssl-dev python-pip sshpass')
        run_shell('sudo apt-get install -y git gcc crudini jq ansible')

    curl(
        '-L',
        'https://bootstrap.pypa.io/get-pip.py',
        '-o', '/tmp/get-pip.py')
    run_shell('sudo python /tmp/get-pip.py')
    # Seems to be the recommended ansible version
    run_shell('sudo -H pip install ansible==%s' % a_ver)
    # Standard jinja2 in Centos7(2.9.6) is broken
    run_shell('sudo -H pip install Jinja2==%s' % j_ver)


def k8s_setup_ntp():
    '''Setup NTP - this caused issues when doing it on a VM'''
    if LINUX == 'Centos':
        run_shell('sudo yum install -y ntp')
        run_shell('sudo systemctl enable ntpd.service')
        run_shell('sudo systemctl start ntpd.service')
    else:
        run_shell('sudo apt-get install -y ntp')
        run_shell('systemctl restart ntp')


def k8s_turn_things_off():
    '''Currently turn off SELinux and Firewall'''
    if LINUX == 'Centos':
        print('Kubernetes - Turn off SELinux')
        run_shell('sudo setenforce 0')
        run_shell('sudo sed -i s/enforcing/permissive/g /etc/selinux/config')

    print('Kubernetes - Turn off firewall')
    if LINUX == 'Centos':
        run_shell('sudo systemctl stop firewalld')
        run_shell('sudo systemctl disable firewalld')
    else:
        run_shell('sudo ufw disable')

        if LINUX == 'Ubuntu':
            print('Kubernetes - Turn off iscsid')
            run_shell('sudo systemctl stop iscsid')
            run_shell('sudo systemctl stop iscsid.service')


def k8s_install_k8s(k8s_version, cni_version):
    '''Necessary repo to install kubernetes and tools
    This is often broken and may need to be more programatic'''
    print('Kubernetes - Creating kubernetes repo')
    run_shell('sudo -H pip install --upgrade pip')
    k8s_create_repo()
    print('Kubernetes - Installing kubernetes packages')
    demo('Installing Kubernetes', 'Installing docker ebtables kubelet-%s kubeadm-%s kubectl-%s kubernetes-cni-%s' %
         (k8s_version, k8s_version, k8s_version, cni_version))

    if LINUX == 'Centos':
        run_shell(
            'sudo yum install -y docker ebtables kubelet-%s kubeadm-%s kubectl-%s \
            kubernetes-cni' % (k8s_version, k8s_version, k8s_version))
    else:
        run_shell('sudo apt-get install -y docker.io ebtables kubelet=%s-00 kubeadm=%s-00 kubectl=%s-00 \
            kubernetes-cni' % (k8s_version, k8s_version, k8s_version))

    if k8s_version == '1.6.3':
        print('Kubernetes - 1.6.3 workaround')
        # 1.6.3 is broken so if user chooses it - use special image
        curl(
            '-L',
            'https://github.com/sbezverk/kubelet--45613/raw/master/kubelet.gz',
            '-o', '/tmp/kubelet.gz')
        run_shell('sudo gunzip -d /tmp/kubelet.gz')
        run_shell('sudo mv -f /tmp/kubelet /usr/bin/kubelet')
        run_shell('sudo chmod +x /usr/bin/kubelet')


def k8s_setup_dns():
    '''DNS services'''
    print('Kubernetes - Start docker and setup the DNS server with the service CIDR')
    run_shell('sudo systemctl enable docker')
    run_shell('sudo systemctl start docker')
    run_shell('sudo cp /etc/systemd/system/kubelet.service.d/10-kubeadm.conf /tmp')
    run_shell('sudo chmod 777 /tmp/10-kubeadm.conf')
    run_shell('sudo sed -i s/10.96.0.10/10.3.3.10/g /tmp/10-kubeadm.conf')
    run_shell('sudo mv /tmp/10-kubeadm.conf /etc/systemd/system/kubelet.service.d/10-kubeadm.conf')


def k8s_reload_service_files():
    '''Service files where modified so bring them up again'''
    print('Kubernetes - Reload the hand-modified service files')
    run_shell('sudo systemctl daemon-reload')


def k8s_start_kubelet():
    '''Start kubelet'''
    print('Kubernetes - Enable and start kubelet')
    demo('Enable and start kubelet', 'kubelet is a command line interface for ' +
         'running commands against Kubernetes clusters')
    run_shell('sudo systemctl enable kubelet')
    run_shell('sudo systemctl start kubelet')


def k8s_fix_iptables():
    '''Maybe Centos only but this needs to be changed to proceed'''
    reload_sysctl = False
    print('Kubernetes - Fix iptables')
    demo('Centos fix bridging',
         'Setting net.bridge.bridge-nf-call-iptables=1 ' +
         'in /etc/sysctl.conf')

    run_shell('sudo cp /etc/sysctl.conf /tmp')
    run_shell('sudo chmod 777 /tmp/sysctl.conf')

    with open('/tmp/sysctl.conf', 'r+') as myfile:
        contents = myfile.read()
        if not re.search('net.bridge.bridge-nf-call-ip6tables=1', contents):
            myfile.write('net.bridge.bridge-nf-call-ip6tables=1' + '\n')
            reload_sysctl = True
        if not re.search('net.bridge.bridge-nf-call-iptables=1', contents):
            myfile.write('net.bridge.bridge-nf-call-iptables=1' + '\n')
            reload_sysctl = True
    if reload_sysctl is True:
        run_shell('sudo mv /tmp/sysctl.conf /etc/sysctl.conf')
        run_shell('sudo sysctl -p')


def k8s_deploy_k8s():
    '''Start the kubernetes master'''
    print('Kubernetes - Deploying Kubernetes with kubeadm')
    demo('Initializes your Kubernetes Master',
         'One of the most frequent criticisms of Kubernetes is that it is ' +
         'hard to install.\n' +
         'Kubeadm is a new tool that is part of the Kubernetes distribution ' +
         'that makes this easier')
    demo('The Kubernetes Control Plane',
         'The Kubernetes control plane consists of the Kubernetes API server\n' +
         '(kube-apiserver), controller manager (kube-controller-manager),\n' +
         'and scheduler (kube-scheduler). The API server depends on etcd so\n' +
         'an etcd cluster is also required.\n' +
         'https://www.ianlewis.org/en/how-kubeadm-initializes-your-kubernetes-master')
    demo('kubeadm and the kubelet',
         'Kubernetes has a component called the Kubelet which manages containers\n' +
         'running on a single host. It allows us to use Kubelet to manage the\n' +
         'control plane components. This is exactly what kubeadm sets us up to do.\n' +
         'We run:\n' +
         'kubeadm init --pod-network-cidr=10.1.0.0/16 --service-cidr=10.3.3.0/24 --skip-preflight-checks and check output\n' +
         'Run: "watch -d sudo docker ps" in another window')
    if DEMO:
        print(run_shell(
            'sudo kubeadm init --pod-network-cidr=10.1.0.0/16 --service-cidr=10.3.3.0/24 --skip-preflight-checks'))
        demo('What happened?',
             'We can see above that kubeadm created the necessary certificates for\n' +
             'the API, started the control plane components, and installed the essential addons.\n' +
             'The join command is important - it allows other nodes to be added to the existing resources\n' +
             'Kubeadm does not mention anything about the Kubelet but we can verify that it is running:')
        print(run_shell('sudo ps aux | grep /usr/bin/kubelet | grep -v grep'))
        demo('Kubelet was started. But what is it doing? ',
             'The Kubelet will monitor the control plane components but what monitors Kubelet and make sure\n' +
             'it is always running? This is where we use systemd. Systemd is started as PID 1 so the OS\n' +
             'will make sure it is always running, systemd makes sure the Kubelet is running, and the\n' +
             'Kubelet makes sure our containers with the control plane components are running.')
    else:
        run_shell(
            'sudo kubeadm init --pod-network-cidr=10.1.0.0/16 --service-cidr=10.3.3.0/24 --skip-preflight-checks')


def k8s_load_kubeadm_creds():
    '''This ensures the user gets output from 'kubectl get pods'''
    print('Kubernetes - Load kubeadm credentials into the system')
    print('Kubernetes - Note "kubectl get pods --all-namespaces" should work now')
    home = os.environ['HOME']
    kube = os.path.join(home, '.kube')
    config = os.path.join(kube, 'config')

    if not os.path.exists(kube):
        os.makedirs(kube)
    run_shell('sudo -H cp /etc/kubernetes/admin.conf %s' % config)
    run_shell('sudo chmod 777 %s' % kube)
    run_shell('sudo -H chown $(id -u):$(id -g) $HOME/.kube/config')
    demo('Verify Kubelet',
         'Kubelete should be running our control plane components and be\n' +
         'connected to the API server (like any other Kubelet node.\n' +
         'Run "watch -d kubectl get pods --all-namespaces" in another window\n' +
         'Note that the kube-dns-* pod is not ready yet. We do not have a network yet')
    demo('Verifying the Control Plane Components',
         'We can see that kubeadm created a /etc/kubernetes/ directory so check\n'
         'out what is there.')
    if DEMO:
        print(run_shell('ls -lh /etc/kubernetes/'))
        demo('Files created by kubectl',
             'The admin.conf and kubelet.conf are yaml files that mostly\n' +
             'contain certs used for authentication with the API. The pki\n' +
             'directory contains the certificate authority certs, API server\n' +
             'certs, and tokens:')
        print(run_shell('ls -lh /etc/kubernetes/pki'))
        demo('The manifests directory ',
             'This directory is where things get interesting. In the\n' +
             'manifests directory we have a number of json files for our\n' +
             'control plane components.')
        print(run_shell('sudo ls -lh /etc/kubernetes/manifests/'))
        demo('Pod Manifests',
             'If you noticed earlier the Kubelet was passed the\n' +
             '--pod-manifest-path=/etc/kubernetes/manifests flag which tells\n' +
             'it to monitor the files in the /etc/kubernetes/manifests directory\n' +
             'and makes sure the components defined therein are always running.\n' +
             'We can see that they are running my checking with the local Docker\n' +
             'to list the running containers.')
        print(run_shell('sudo docker ps --format="table {{.ID}}\t{{.Image}}"'))
        demo('Note above containers',
             'We can see that etcd, kube-apiserver, kube-controller-manager, and\n' +
             'kube-scheduler are running.')
        demo('How can we connect to containers?',
             'If we look at each of the json files in the /etc/kubernetes/manifests\n' +
             'directory we can see that they each use the hostNetwork: true option\n' +
             'which allows the applications to bind to ports on the host just as\n' +
             'if they were running outside of a container.')
        demo('Connect to the API',
             'So we can connect to the API servers insecure local port.\n' +
             'curl http://127.0.0.1:8080/version')
        print(run_shell('sudo curl http://127.0.0.1:8080/version'))
        demo('Secure port?', 'The API server also binds a secure port 443 which\n' +
             'requires a client cert and authentication. Be careful to use the\n' +
             'public IP for your master here.\n' +
             'curl --cacert /etc/kubernetes/pki/ca.pem https://10.240.0.2/version')
        print(run_shell('curl --cacert /etc/kubernetes/pki/ca.pem https://10.240.0.2/version'))


def k8s_deploy_canal_sdn():
    '''SDN/CNI Driver of choice is Canal'''
    # The ip range in canal.yaml,
    # /etc/kubernetes/manifests/kube-controller-manager.yaml and the kubeadm
    # init command must match
    print('Kubernetes - Create RBAC')
    answer = curl(
        '-L',
        'https://raw.githubusercontent.com/projectcalico/canal/master/k8s-install/1.6/rbac.yaml',
        '-o', '/tmp/rbac.yaml')
    logger.debug(answer)
    run_shell('kubectl create -f /tmp/rbac.yaml')

    print('Kubernetes - Deploy the Canal CNI driver')
    if DEMO:
        demo('Why use a CNI Driver?',
             'Container Network Interface (CNI) is a specification started by CoreOS\n' +
             'with the input from the wider open source community aimed to make network\n' +
             'plugins interoperable between container execution engines. It aims to be\n' +
             'as common and vendor-neutral as possible to support a wide variety of\n' +
             'networking options from MACVLAN to modern SDNs such as Weave and flannel.\n\n' +
             'CNI is growing in popularity. It got its start as a network plugin\n' +
             'layer for rkt, a container runtime from CoreOS. CNI is getting even\n' +
             'wider adoption with Kubernetes adding support for it. Kubernetes\n' +
             'accelerates development cycles while simplifying operations, and with\n' +
             'support for CNI is taking the next step toward a common ground for\n' +
             'networking.')
    answer = curl(
        '-L',
        'https://raw.githubusercontent.com/projectcalico/canal/master/k8s-install/1.6/canal.yaml',
        '-o', '/tmp/canal.yaml')
    logger.debug(answer)
    run_shell('sudo chmod 777 /tmp/canal.yaml')
    run_shell('sudo sed -i s@10.244.0.0/16@10.1.0.0/16@ /tmp/canal.yaml')
    run_shell('kubectl create -f /tmp/canal.yaml')
    demo('Wait for CNI to be deployed',
         'A successfully deployed CNI will result in a valid dns pod')


def k8s_add_api_server(ip):
    print('Kubernetes - Add API Server')
    run_shell('sudo mkdir -p /etc/nodepool/')
    run_shell('sudo echo %s > /tmp/primary_node_private' % ip)
    # todo - has a permissions error
    run_shell('sudo mv -f /tmp/primary_node_private /etc/nodepool')


def k8s_schedule_master_node():
    '''Normally master node won't be happy - unless you do this step to
    make it an AOI deployment

    While the command says "taint" the "-" at the end is an "untaint"'''
    print('Kubernetes - Mark master node as schedulable')
    demo('Running on the master is different though',
         'There is a special annotation on our node telling Kubernetes not to\n' +
         'schedule containers on our master node.')
    run_shell('kubectl taint nodes --all=true node-role.kubernetes.io/master:NoSchedule-')


def kolla_update_rbac():
    '''Override the default RBAC settings'''
    print('Kolla - Overide default RBAC settings')
    demo('Role-based access control (RBAC)',
         'A method of regulating access to computer or network resources based\n' +
         'on the roles of individual users within an enterprise. In this context,\n' +
         'access is the ability of an individual user to perform a specific task\n' +
         'such as view, create, or modify a file.')
    name = '/tmp/rbac'
    with open(name, "w") as w:
        w.write("""\
apiVersion: rbac.authorization.k8s.io/v1alpha1
kind: ClusterRoleBinding
metadata:
  name: cluster-admin
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- kind: Group
  name: system:masters
- kind: Group
  name: system:authenticated
- kind: Group
  name: system:unauthenticated
""")
    if DEMO:
        print(run_shell('kubectl update -f /tmp/rbac'))
        demo('Note the cluster-admin has been replaced', '')
    else:
        run_shell('kubectl update -f /tmp/rbac')


def kolla_install_deploy_helm(version):
    '''Deploy helm binary'''
    print('Kolla - Install and deploy Helm version %s - Tiller pod' % version)
    demo('Download the version of helm requested and install it',
         'Installing means the Tiller Server will be instantiated in a pod')
    # url = 'https://raw.githubusercontent.com/kubernetes/helm/master/scripts/get'
    # curl('-sSL', url, '-o', '/tmp/get_helm.sh')
    # run_shell('chmod 700 /tmp/get_helm.sh')
    # run_shell('/tmp/get_helm.sh')
    url = 'https://storage.googleapis.com/kubernetes-helm/helm-v%s-linux-amd64.tar.gz' % version
    curl('-sSL', url, '-o', '/tmp/helm-v%s-linux-amd64.tar.gz' % version)
    untar('/tmp/helm-v%s-linux-amd64.tar.gz' % version)
    run_shell('sudo mv -f linux-amd64/helm /usr/local/bin/helm')
    run_shell('helm init')
    k8s_wait_for_running_negate()
    # Check for helm version
    # Todo - replace this to using json path to check for that field
    while True:
        out = run_shell('helm version | grep "%s" | wc -l' % version)
        if int(out) == 2:
            print('Kolla - Helm successfully installed')
            break
        else:
            time.sleep(3)
            continue
    demo('Check running pods..',
         'Note that the helm version in server and client is the same.\n' +
         'Tiller is ready to respond to helm chart requests')


def k8s_cleanup(doit):
    '''Cleanup on Isle 9'''
    if doit is True:
        print('Kubernetes - Cleaning up existing Kubernetes Cluster')
        run_shell('sudo kubeadm reset')
        print('Kubernetes - Cleaning up old directories and files and docker images')
        # run_shell('sudo docker stop $(sudo docker ps -a | grep k8s| cut -c1-20 | xargs sudo docker stop)')
        # run_shell('sudo docker rm -f $(sudo docker ps -a | grep k8s| cut -c1-20
        # | xargs sudo docker stop)')
        run_shell('sudo rm -rf /etc/kolla*')
        run_shell('sudo rm -rf /etc/kubernetes')
        run_shell('sudo rm -rf /etc/kolla-kubernetes')
        run_shell('sudo rm -rf /var/lib/kolla*')
        run_shell('sudo rm -rf /tmp/*')
        run_shell('sudo rm -rf /var/etcd')
        run_shell('sudo rm -rf /var/run/kubernetes/*')
        run_shell('sudo rm -rf /var/lib/kubelet/*')
        run_shell('sudo rm -rf /var/run/lock/kubelet.lock')
        run_shell('sudo rm -rf /var/run/lock/api-server.lock')
        run_shell('sudo rm -rf /var/run/lock/etcd.lock')
        run_shell('sudo rm -rf /var/run/lock/kubelet.lock')
        if os.path.exists('/data'):
            print('Kubernetes - Remove cinder volumes and data')
            run_shell('sudo vgremove cinder-volumes')
            run_shell('sudo rm -rf /data')


def kolla_install_repos():
    '''Installing the kolla repos
    For sanity I just delete a repo if already exists'''
    print('Kolla - Clone kolla-ansible')
    demo('Git cloning repos, then using pip to install them',
         'http://github.com/openstack/kolla-ansible\n' +
         'http://github.com/openstack/kolla-kubernetes')

    if os.path.exists('./kolla-ansible'):
        run_shell('sudo rm -rf ./kolla-ansible')
    run_shell('git clone http://github.com/openstack/kolla-ansible')

    print('Kolla - Clone kolla-kubernetes')
    if os.path.exists('./kolla-kubernetes'):
        run_shell('sudo rm -rf ./kolla-kubernetes')
    run_shell('git clone http://github.com/openstack/kolla-kubernetes')

    print('Kolla - Install kolla-ansible and kolla-kubernetes')
    run_shell('sudo -H pip install -U kolla-ansible/ kolla-kubernetes/')

    if LINUX == 'Centos':
        print('Kolla - Copy default kolla-ansible configuration to /etc')
        run_shell('sudo cp -aR /usr/share/kolla-ansible/etc_examples/kolla /etc')
    else:
        print('Kolla - Copy default kolla-ansible configuration to /etc')
        run_shell('sudo cp -aR /usr/local/share/kolla-ansible/etc_examples/kolla /etc')

    print('Kolla - Copy default kolla-kubernetes configuration to /etc')
    run_shell('sudo cp -aR kolla-kubernetes/etc/kolla-kubernetes /etc')


def kolla_setup_loopback_lvm():
    '''Setup a loopback LVM for Cinder
    /opt/kolla-kubernetes/tests/bin/setup_gate_loopback_lvm.sh'''
    print('Kolla - Setup Loopback LVM for Cinder')
    demo('Loopback LVM for Cinder',
         'Create a flat file on the filesystem and then loopback mount\n' +
         'it so that it looks like a block-device attached to /dev/zero\n' +
         'Then LVM manages it. This is useful for test and development\n' +
         'It is also very slow and you will see etcdserver time out frequently')
    new = '/tmp/setup_lvm'
    with open(new, "w") as w:
        w.write("""
sudo mkdir -p /data/kolla
sudo df -h
sudo dd if=/dev/zero of=/data/kolla/cinder-volumes.img bs=5M count=2048
LOOP=$(losetup -f)
sudo losetup $LOOP /data/kolla/cinder-volumes.img
sudo parted -s $LOOP mklabel gpt
sudo parted -s $LOOP mkpart 1 0% 100%
sudo parted -s $LOOP set 1 lvm on
sudo partprobe $LOOP
sudo pvcreate -y $LOOP
sudo vgcreate -y cinder-volumes $LOOP
""")
    run_shell('bash %s' % new)


def kolla_install_os_client():
    '''Install Openstack Client'''
    print('Kolla - Install Python Openstack Client')
    demo('Install Python packages',
         'python-openstackclient, python-neutronclient and python-cinderclient\n' +
         'provide the command-line clients for openstack')
    run_shell('sudo -H pip install python-openstackclient')
    run_shell('sudo -H pip install python-neutronclient')
    run_shell('sudo -H pip install python-cinderclient')


def kolla_gen_passwords():
    '''Generate the Kolla Passwords'''
    print('Kolla - Generate default passwords via SPRNG')
    demo('Generate passwords',
         'This will populate all empty fields in the /etc/kolla/passwords.yml\n' +
         'file using randomly generated values to secure the deployment')
    run_shell('sudo kolla-kubernetes-genpwd')


def kolla_create_namespace():
    '''Create a kolla namespace'''
    print('Kolla - Create a Kubernetes namespace to isolate this Kolla deployment')
    demo('Isolate the Kubernetes namespace',
         'Create a namespace using "kubectl create namespace kolla"')
    if DEMO:
        print(run_shell('kubectl create namespace kolla'))
    else:
        run_shell('kubectl create namespace kolla')


def k8s_label_nodes(node_list):
    '''Label the nodes according to the list passed in'''
    demo('Label the node',
         'Currently controller and compute')
    for node in node_list:
        print('Kolla - Label the AIO node as %s' % node)
        run_shell('kubectl label node $(hostname) %s=true' % node)


def k8s_check_exit(k8s_only):
    '''If the user only wants kubernetes and not kolla - stop here'''
    if k8s_only is True:
        print('Kubernetes Cluster is running and healthy and you do not wish to install kolla')
        sys.exit(1)


def kolla_modify_globals(MGMT_INT, MGMT_IP, NEUTRON_INT):
    '''Necessary additions and changes to the global.yml - which is based on
    the users inputs'''
    print('Kolla - Modify globals to setup network_interface and neutron_interface')
    demo('Kolla uses two files currently to configure',
         'Here we are modifying /etc/kolla/globals.yml\n' +
         'We are setting the management interface to "%s" and IP to %s\n' % (MGMT_INT, MGMT_IP) +
         'The interface for neutron(externally bound) "%s"\n' % NEUTRON_INT +
         'globals.yml is used when we run ansible to generate configs in further step')
    run_shell("sudo sed -i 's/eth0/%s/g' /etc/kolla/globals.yml" % MGMT_INT)
    run_shell("sudo sed -i 's/#network_interface/network_interface/g' /etc/kolla/globals.yml")
    run_shell("sudo sed -i 's/10.10.10.254/%s/g' /etc/kolla/globals.yml" % MGMT_IP)
    run_shell("sudo sed -i 's/eth1/%s/g' /etc/kolla/globals.yml" % NEUTRON_INT)
    run_shell("sudo sed -i 's/#neutron_external_interface/neutron_external_interface/g' /etc/kolla/globals.yml")


def kolla_add_to_globals():
    '''Default section needed'''
    print('Kolla - Add default config to globals.yml')

    new = '/tmp/add'
    add_to = '/etc/kolla/globals.yml'

    with open(new, "w") as w:
        w.write("""
kolla_install_type: "source"
tempest_image_alt_id: "{{ tempest_image_id }}"
tempest_flavor_ref_alt_id: "{{ tempest_flavor_ref_id }}"

neutron_plugin_agent: "openvswitch"
api_interface_address: 0.0.0.0
tunnel_interface_address: 0.0.0.0
orchestration_engine: KUBERNETES
memcached_servers: "memcached"
keystone_admin_url: "http://keystone-admin:35357/v3"
keystone_internal_url: "http://keystone-internal:5000/v3"
keystone_public_url: "http://keystone-public:5000/v3"
glance_registry_host: "glance-registry"
neutron_host: "neutron"
keystone_database_address: "mariadb"
glance_database_address: "mariadb"
nova_database_address: "mariadb"
nova_api_database_address: "mariadb"
neutron_database_address: "mariadb"
cinder_database_address: "mariadb"
ironic_database_address: "mariadb"
placement_database_address: "mariadb"
rabbitmq_servers: "rabbitmq"
openstack_logging_debug: "True"
enable_haproxy: "no"
enable_heat: "no"
enable_cinder: "yes"
enable_cinder_backend_lvm: "yes"
enable_cinder_backend_iscsi: "yes"
enable_cinder_backend_rbd: "no"
enable_ceph: "no"
enable_elasticsearch: "no"
enable_kibana: "no"
glance_backend_ceph: "no"
cinder_backend_ceph: "no"
nova_backend_ceph: "no"
""")
    run_shell('cat %s | sudo tee -a %s' % (new, add_to))
    demo('We have also added some basic config that is not defaulted',
         'Mainly Cinder and Database:')
    if DEMO:
        print(run_shell('sudo cat /tmp/add'))


def kolla_enable_qemu():
    '''Some configurations need qemu'''
    print('Kolla - Enable qemu')
    # todo - as per gate:
    # sudo crudini --set /etc/kolla/nova-compute/nova.conf libvirt virt_type qemu
    # sudo crudini --set /etc/kolla/nova-compute/nova.conf libvirt cpu_mode none
    # sudo crudini --set /etc/kolla/keystone/keystone.conf cache enabled False

    run_shell('sudo mkdir -p /etc/kolla/config')

    new = '/tmp/add'
    add_to = '/etc/kolla/config/nova.conf'
    with open(new, "w") as w:
        w.write("""
[libvirt]
virt_type = qemu
cpu_mode = none
""")
    run_shell('sudo mv %s %s' % (new, add_to))


def kolla_gen_configs():
    '''Generate the configs using Jinja2
    Some version meddling here until things are more stable'''
    print('Kolla - Generate the default configuration')
    # globals.yml is used when we run ansible to generate configs
    demo('Explantion about generating configs',
         'There is absolutely no written description about the following steps: gen config and configmaps...\n' +
         'The default configuration is generated by Ansible using the globals.yml and the generated password\n' +
         'into files in /etc/kolla\n' +
         '"kubectl create configmap" is called to wrap each microservice config into a configmap.\n' +
         'When helm microchart is launched, it mounts the configmap into the container via a\n ' +
         'tmpfs bindmount and the configuration is read and processed by the microcharts\n' +
         'container and the container then does its thing')

    demo('The command executed is',
         'cd kolla-kubernetes; sudo ansible-playbook -e \
         ansible_python_interpreter=/usr/bin/python -e \
         @/etc/kolla/globals.yml -e @/etc/kolla/passwords.yml \
         -e CONFIG_DIR=/etc/kolla ./ansible/site.yml')

    demo('This is temporary',
         'The next gen involves creating config maps in helm charts with overides (sound familiar?)')

    run_shell('cd kolla-kubernetes; sudo ansible-playbook -e \
    ansible_python_interpreter=/usr/bin/python -e \
    @/etc/kolla/globals.yml -e @/etc/kolla/passwords.yml \
    -e CONFIG_DIR=/etc/kolla ./ansible/site.yml; cd ..')


def kolla_gen_secrets():
    '''Generate Kubernetes secrets'''
    print('Kolla - Generate the Kubernetes secrets and register them with Kubernetes')
    demo('Create secrets from the generated password file using "kubectl create secret generic"',
         'Kubernetes Secrets is an object that contains a small amount of\n' +
         'sensitive data such as passwords, keys and tokens etc')
    run_shell('python ./kolla-kubernetes/tools/secret-generator.py create')


def kolla_create_config_maps():
    '''Generate the Kolla config map'''
    print('Kolla - Create and register the Kolla config maps')
    demo('Create Kolla Config Maps',
         'Similar to Secrets, Config Maps are another kubernetes artifact\n' +
         'ConfigMaps allow you to decouple configuration artifacts from image\n' +
         'content to keep containerized applications portable. The ConfigMap API\n' +
         'resource stores configuration data as key-value pairs. The data can be\n' +
         'consumed in pods or provide the configurations for system components\n' +
         'such as controllers. ConfigMap is similar to Secrets, but provides a\n' +
         'means of working with strings that do not contain sensitive information.\n' +
         'Users and system components alike can store configuration data in ConfigMap.')
    run_shell('kollakube res create configmap \
    mariadb keystone horizon rabbitmq memcached nova-api nova-conductor \
    nova-scheduler glance-api-haproxy glance-registry-haproxy glance-api \
    glance-registry neutron-server neutron-dhcp-agent neutron-l3-agent \
    neutron-metadata-agent neutron-openvswitch-agent openvswitch-db-server \
    openvswitch-vswitchd nova-libvirt nova-compute nova-consoleauth \
    nova-novncproxy nova-novncproxy-haproxy neutron-server-haproxy \
    nova-api-haproxy cinder-api cinder-api-haproxy cinder-backup \
    cinder-scheduler cinder-volume iscsid tgtd keepalived \
    placement-api placement-api-haproxy')

    demo('Lets look at a configmap',
         'kubectl get configmap -n kolla; kubectl describe configmap -n kolla XYZ')


def kolla_resolve_workaround():
    '''Resolve.Conf workaround'''
    print('Kolla - Enable resolv.conf workaround')
    run_shell('./kolla-kubernetes/tools/setup-resolv-conf.sh kolla')


def kolla_build_micro_charts():
    '''Build all helm micro charts'''
    print('Kolla - Build all Helm microcharts, service charts, and metacharts')
    demo('Build helm charts',
         'Helm uses a packaging format called charts. A chart is a collection of\n' +
         'files that describe a related set of Kubernetes resources. A single chart\n' +
         'might be used to deploy something simple, like a memcached pod, or something\n' +
         'complex, like a full web app stack with HTTP servers, databases, caches, and so on\n' +
         'Helm also allows you to detail dependencies between charts - vital for Openstack\n' +
         'This step builds all the known helm charts and dependencies (193)\n' +
         'This is another step that takes a few minutes')
    if DEMO:
        print(run_shell('./kolla-kubernetes/tools/helm_build_all.sh /tmp'))
    else:
        run_shell('./kolla-kubernetes/tools/helm_build_all.sh /tmp')

        demo('Lets look at these helm charts',
             'helm list; helm search | grep local | wc -l; helm fetch url chart; helm inspect local/glance')


def kolla_verify_helm_images():
    '''Subjective but a useful check to see if enough helm charts were
    generated'''
    out = run_shell('ls /tmp | grep ".tgz" | wc -l')
    if int(out) > 190:
        print('Kolla - %s Helm images created' % int(out))
    else:
        print('Kolla - Error: only %s Helm images created' % int(out))
        sys.exit(1)


def kolla_create_cloud(args):
    '''Generate the cloud.yml file which works with the globals.yml
    file to define your cluster networking.

    This uses most of the user options.'''
    print('Kolla - Create a cloud.yaml')

    demo('Create a cloud.yaml',
         'cloud.yaml is the partner to globals.yml\n' +
         'It contains a list of global OpenStack services and key-value pairs, which\n' +
         'guide helm when running each chart. This includes our basic inputs, MGMT and Neutron')
    cloud = '/tmp/cloud.yaml'
    with open(cloud, "w") as w:
        w.write("""
global:
   kolla:
     all:
       image_tag: "%s"
       kube_logger: false
       external_vip: "%s"
       base_distro: "centos"
       install_type: "source"
       tunnel_interface: "%s"
       resolve_conf_net_host_workaround: true
       kolla_kubernetes_external_subnet: 24
       kolla_kubernetes_external_vip: %s
       kube_logger: false
     keepalived:
       all:
         api_interface: br-ex
     keystone:
       all:
         admin_port_external: "true"
         dns_name: "%s"
         port: 5000
       public:
         all:
           port_external: "true"
     rabbitmq:
       all:
         cookie: 67
     glance:
       api:
         all:
           port_external: "true"
     cinder:
       api:
         all:
           port_external: "true"
       volume_lvm:
         all:
           element_name: cinder-volume
         daemonset:
           lvm_backends:
           - '%s': 'cinder-volumes'
     ironic:
       conductor:
         daemonset:
           selector_key: "kolla_conductor"
     nova:
       placement_api:
         all:
           port_external: true
       novncproxy:
         all:
           port: 6080
           port_external: true
     openvwswitch:
       all:
         add_port: true
         ext_bridge_name: br-ex
         ext_interface_name: %s
         setup_bridge: true
     horizon:
       all:
         port_external: true
        """ % (args.image_tag, args.MGMT_IP, args.MGMT_INT, args.VIP_IP,
               args.MGMT_IP, args.MGMT_IP, args.NEUTRON_INT))

    if DEMO:
        print(run_shell('sudo cat /tmp/cloud.yaml'))


def helm_install_service_chart(chart_list):
    '''helm install a list of service charts'''
    for chart in chart_list:
        print('Helm - Install service chart: %s' % chart)
        run_shell('helm install --debug kolla-kubernetes/helm/service/%s \
        --namespace kolla --name %s --values /tmp/cloud.yaml' % (chart, chart))
    k8s_wait_for_running_negate()


def helm_install_micro_service_chart(chart_list):
    '''helm install a list of micro service charts'''
    for chart in chart_list:
        print('Helm - Install service chart: %s' % chart)
        run_shell('helm install --debug kolla-kubernetes/helm/microservice/%s \
        --namespace kolla --name %s --values /tmp/cloud.yaml' % (chart, chart))
    k8s_wait_for_running_negate()


def sudo_timeout_off(state):
    '''Turn sudo timeout off or on'''
    # if state is True:
    # d = run_shell('sudo echo "Defaults timestamp_timeout=-1" >> /etc/sudoers')
    # sudo sh -c 'echo "Defaults timestamp_timeout=-1" >> /etc/sudoers'
    # print(d)
    # else:
    # d = run_shell('sudo sed -i "/Defaults timestamp_timeout=-1/d" /etc/sudoers')
    # print(d)


def kolla_create_demo_vm():
    '''Final steps now that a working cluster is up.
    Create a keystone admin user.
    Run "runonce" to set everything up and then install a demo image.
    Attach a floating ip'''

    demo('We now should have a running OpenStack Cluster on Kubernetes!',
         'Lets create a keystone account, create a demo VM, attach a floating ip\n' +
         'Finally ssh to the VM and or open Horizon and see our cluster')
    print('Kolla - Create a keystone admin account and source in to it')
    run_shell('sudo rm -f ~/keystonerc_admin')
    run_shell('kolla-kubernetes/tools/build_local_admin_keystonerc.sh ext')
    out = run_shell('.  ~/keystonerc_admin; kolla-ansible/tools/init-runonce')
    logger.debug(out)

    demo_net_id = run_shell(".  ~/keystonerc_admin; \
    echo $(openstack network list | awk '/ demo-net / {print $2}')")
    logger.debug(demo_net_id)

    # Create a demo image
    print('Kolla - Create a demo vm in our OpenStack cluster')
    create_demo1 = 'openstack server create --image cirros \
    --flavor m1.tiny --key-name mykey --nic net-id=%s demo1' % demo_net_id.rstrip()
    run_shell('.  ~/keystonerc_admin; %s' % create_demo1)
    k8s_wait_for_vm('demo1')

    # Create a floating ip
    print('Kolla - Create floating ip')
    cmd = ".  ~/keystonerc_admin; \
    openstack server add floating ip demo1 $(openstack floating ip \
    create public1 -f value -c floating_ip_address)"
    run_shell(cmd)

    # Open up ingress rules to access VM
    print('Kolla - Allow Ingress by changing neutron rules')
    new = '/tmp/neutron_rules.sh'
    with open(new, "w") as w:
        w.write("""
openstack security group list -f value -c ID | while read SG_ID; do
    neutron security-group-rule-create --protocol icmp \
        --direction ingress $SG_ID
    neutron security-group-rule-create --protocol tcp \
        --port-range-min 22 --port-range-max 22 \
        --direction ingress $SG_ID
done
""")
    run_shell('.  ~/keystonerc_admin; chmod 766 %s; bash %s' % (new, new))

    # Display nova list
    print('Kolla - nova list')
    print(run_shell('.  ~/keystonerc_admin; nova list'))
    # todo: ssh execute to ip address and ping google

    # Suggest Horizon logon info
    address = run_shell(
        "kubectl get svc horizon --namespace kolla --no-headers | awk '{print $3}'")
    username = run_shell("cat ~/keystonerc_admin | grep OS_PASSWORD | awk '{print $2}'")
    password = run_shell("cat ~/keystonerc_admin | grep OS_USERNAME | awk '{print $2}'")
    print('To Access Horizon:')
    print('  Point your browser to: %s' % address)
    print('  %s' % username)
    print('  %s' % password)


def k8s_test_neutron_int(ip):
    '''Test that the neutron interface is not used'''
    if LINUX == 'Centos':
        run_shell('sudo yum install -y nmap')
    else:
        run_shell('sudo apt-get install -y nmap')

    truth = run_shell('sudo nmap -sP -PR %s | grep Host' % ip)
    if re.search('Host is up', truth):
        print('Kubernetes - Neutron Interface %s is in use, choose another' % ip)
        sys.exit(1)
    else:
        print('Kubernetes - VIP Keepalive Interface %s is valid' % ip)


def k8s_get_pods(namespace):
    '''Display all pods per namespace list'''
    for name in namespace:
        final = run_shell('kubectl get pods -n %s' % name)
        print('Kolla - Final Kolla Kubernetes Openstack pods for namespace %s:' % name)
        print(final)


def k8s_pause_to_check_nslookup(manual_check):
    '''Create a test pod and query nslookup against kubernetes
    Only seems to work in the default namespace

    Also handles the option to create a test pod manually like
    the deployment guide advises.'''
    print("Kubernetes - Test 'nslookup kubernetes'")
    demo('Lets create a simple pod and verify that DNS works',
         'If it does not then this deployment will not work.')
    name = './busybox.yaml'
    with open(name, "w") as w:
        w.write("""\
apiVersion: v1
kind: Pod
metadata:
  name: kolla-dns-test
spec:
  containers:
  - name: busybox
    image: busybox
    args:
    - sleep
    - "1000000"
""")
    demo('The busy box yaml is: %s' % name, '')
    if DEMO:
        print(run_shell('sudo cat ./busybox.yaml'))

    run_shell('kubectl create -f %s' % name)
    k8s_wait_for_running_negate()
    out = run_shell(
        'kubectl exec kolla-dns-test -- nslookup kubernetes | grep -i address | wc -l')
    demo('Kolla DNS test output: "%s"' % out, '')
    if int(out) != 2:
        print("Kubernetes - Warning 'nslookup kubernetes ' failed. YMMV continuing")
    else:
        print("Kubernetes - 'nslookup kubernetes' worked - continuing")

    # run_shell('kubectl delete kolla-dns-test -n default') # todo - doesn't delete

    if manual_check:
        print('Kubernetes - Run the following to create a pod to test kubernetes nslookup')
        print('Kubernetes - kubectl run -i -t $(uuidgen) --image=busybox --restart=Never')
        pause_to_debug('Check "nslookup kubernetes" now')


def kubernetes_test_cli():
    '''Run some commands for demo purposes'''
    if not DEMO:
        return

    demo('Test CLI:', 'Determine IP and port information from Service:')
    print(run_shell('kubectl get svc -n kube-system'))
    print(run_shell('kubectl get svc -n kolla'))

    demo('Test CLI:', 'View all k8s namespaces:')
    print(run_shell('kubectl get namespaces'))

    demo('Test CLI:', 'Kolla Describe a pod in full detail:')
    print(run_shell('kubectl describe pod ceph-admin -n kolla'))

    demo('Test CLI:', 'View all deployed services:')
    print(run_shell('kubectl get deployment -n kube-system'))

    demo('Test CLI:', 'View configuration maps:')
    print(run_shell('kubectl get configmap -n kube-system'))

    demo('Test CLI:', 'General Cluster information:')
    print(run_shell('kubectl cluster-info'))

    demo('Test CLI:', 'View all jobs:')
    print(run_shell('kubectl get jobs --all-namespaces'))

    demo('Test CLI:', 'View all deployments:')
    print(run_shell('kubectl get deployments --all-namespaces'))

    demo('Test CLI:', 'View secrets:')
    print(run_shell('kubectl get secrets'))

    demo('Test CLI:', 'View docker images')
    print(run_shell('sudo docker images'))

    demo('Test CLI:', 'View deployed Helm Charts')
    print(run_shell('helm list'))

    demo('Test CLI:', 'Working cluster kill a pod and watch resilience.')
    demo('Test CLI:', 'kubectl delete pods <name> -n kolla')


def k8s_bringup_kubernetes_cluster(args):
    '''Bring up a working Kubernetes Cluster
    Explicitly using the Canal CNI for now'''
    if args.openstack:
        print('Kolla - Building OpenStack on existing Kubernetes cluster')
        return

    k8s_install_tools(args.ansible_version, args.jinja2_version)
    k8s_cleanup(args.cleanup)
    print('Kubernetes - Bring up a Kubernetes Cluster')
    k8s_setup_ntp()
    k8s_turn_things_off()
    k8s_install_k8s(args.k8s_version, args.cni_version)
    k8s_setup_dns()
    k8s_reload_service_files()
    k8s_start_kubelet()
    k8s_fix_iptables()
    k8s_deploy_k8s()
    k8s_load_kubeadm_creds()
    k8s_wait_for_kube_system()
    k8s_add_api_server(args.MGMT_IP)
    k8s_deploy_canal_sdn()
    k8s_wait_for_running_negate()
    k8s_schedule_master_node()
    k8s_pause_to_check_nslookup(args.nslookup)
    k8s_check_exit(args.kubernetes)
    demo('Congrats - your kubernetes cluster should be up and running now', '')


def kolla_bring_up_openstack(args):
    '''Install OpenStack with Kolla'''
    print('Kolla - install OpenStack')
    # Start Kolla deployment
    kolla_update_rbac()
    kolla_install_deploy_helm(args.helm_version)
    kolla_install_repos()
    kolla_setup_loopback_lvm()
    kolla_install_os_client()
    kolla_gen_passwords()
    kolla_create_namespace()

    # Label AOI as Compute and Controller nodes
    node_list = ['kolla_compute', 'kolla_controller']
    k8s_label_nodes(node_list)

    kolla_modify_globals(args.MGMT_INT, args.MGMT_IP, args.NEUTRON_INT)
    kolla_add_to_globals()
    kolla_enable_qemu()
    kolla_gen_configs()
    kolla_gen_secrets()
    kolla_create_config_maps()
    kolla_resolve_workaround()
    kolla_build_micro_charts()
    kolla_verify_helm_images()
    kolla_create_cloud(args)

    # Set up OVS for the Infrastructure
    chart_list = ['openvswitch']
    demo('Install %s Helm Chart' % chart_list, '')
    helm_install_service_chart(chart_list)

    chart_list = ['keepalived-daemonset']
    demo('Install %s Helm Chart' % chart_list, '')
    helm_install_micro_service_chart(chart_list)

    # Install Helm charts
    chart_list = ['mariadb']
    demo('Install %s Helm Chart' % chart_list, '')
    helm_install_service_chart(chart_list)

    # Install remaining service level charts
    chart_list = ['rabbitmq', 'memcached', 'keystone', 'glance',
                  'cinder-control', 'cinder-volume-lvm', 'horizon',
                  'neutron']
    demo('Install %s Helm Chart' % chart_list, '')
    helm_install_service_chart(chart_list)

    chart_list = ['nova-control', 'nova-compute']
    demo('Install %s Helm Chart' % chart_list, '')
    helm_install_service_chart(chart_list)

    namespace_list = ['kube-system', 'kolla']
    k8s_get_pods(namespace_list)


def main():
    '''Main function.'''
    args = parse_args()

    global DEBUG
    DEBUG = args.verbose

    global DEMO
    DEMO = args.demo

    global FORCE
    FORCE = args.force

    print_versions(args)

    set_logging()
    logger.setLevel(level=args.verbose)

    try:
        if args.complete_cleanup:
            k8s_cleanup(args.complete_cleanup)
            print('Cleanup - Complete Cleanup done. Highly recommend rebooting your host')
            sys.exit(1)

        k8s_test_neutron_int(args.VIP_IP)
        k8s_bringup_kubernetes_cluster(args)
        kolla_bring_up_openstack(args)
        kolla_create_demo_vm()
        kubernetes_test_cli()

    except Exception:
        print('Exception caught:')
        print(sys.exc_info())
        raise


if __name__ == '__main__':
    main()
