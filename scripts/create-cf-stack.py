#! /usr/bin/python -tt

from paramiko import SSHClient
from boto import cloudformation
from boto import regioninfo
from boto import ec2
import argparse
import ConfigParser
import time
import logging
import sys
import random
import string
import json
import tempfile
import paramiko
import yaml


class SyncSSHClient(SSHClient):
    '''
    Special class for sync'ed commands execution over ssh
    '''
    def run_sync(self, command):
        logging.debug("RUN_SYNC '%s'" % command)
        stdin, stdout, stderr = self.exec_command(command)
        status = stdout.channel.recv_exit_status()
        if status:
            logging.debug("RUN_SYNC status: %i" % status)
        else:
            logging.debug("RUN_SYNC failed!")
        return stdin, stdout, stderr

    def run_with_pty(self, command):
        logging.debug("RUN_WITH_PTY '%s'" % command)
        chan = self.get_transport().open_session()
        chan.get_pty()
        chan.exec_command(command)
        status = chan.recv_exit_status()
        logging.debug("RUN_WITH_PTY recv: %s" % chan.recv(16384))
        logging.debug("RUN_WITH_PTY status: %i" % status)
        chan.close()
        return status


def setup_host_ssh(hostname, key):
    '''
    Setup ssh connection to host.
    If necessary allow root ssh connections
    '''
    ntries = 20
    sftp = None
    client = SyncSSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    custom_users=['ec2-user', 'fedora']
    while ntries > 0:
        try:
            logging.debug("Trying to connect to %s as root" % hostname)
            client.connect(hostname=hostname,
                           username="root",
                           key_filename=key,
                           look_for_keys=False)
            stdin, stdout, stderr = client.run_sync("whoami")
            output = stdout.read().strip()
            logging.debug("OUTPUT for 'whoami': " + output)
            if output != "root":
                #It's forbidden to login under 'root', switching this off
                user = custom_users[ntries % len(custom_users)]
                client = SyncSSHClient()
                client.load_system_host_keys()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(hostname=hostname,
                               username=user,
                               key_filename=key,
                               look_for_keys=False)
                client.run_with_pty("sudo su -c 'cp -af /home/%s/.ssh/authorized_keys /root/.ssh/authorized_keys; chown root.root /root/.ssh/authorized_keys'" % user)
                client.run_with_pty("sudo su -c \"sed -i 's,disable_root: 1,disable_root: 0,' /etc/cloud/cloud.cfg\"")
                client.connect(hostname=hostname,
                               username="root",
                               key_filename=key,
                               look_for_keys=False)
            sftp = client.open_sftp()
            break
        except Exception, e:
            logging.debug('Caught exception in setup_host_ssh: ' + str(e.__class__) + ': ' + str(e))
            ntries -= 1
        time.sleep(10)
    if ntries == 0:
        logging.error("Failed to setup ssh to " + hostname + " using " + key + " key")
    return (client, sftp)


def setup_master(client):
    '''
    Create ssh key on master node.
    '''
    try:
        client.run_sync("rm -f /root/.ssh/id_rsa{,.pub}; ssh-keygen -t rsa -b 2048 -N '' -f /root/.ssh/id_rsa")
        stdin, stdout, stderr = client.run_sync("cat /root/.ssh/id_rsa.pub")
        output = stdout.read()
        logging.debug("Generated ssh master key: " + output)
        return output
    except Exception, e:
        logging.error('Caught exception in setup_master: ' + str(e.__class__) + ': ' + str(e))
        return None


def setup_slave(client, sftp, hostname, hostsfile, yamlfile, master_keys, setup_script):
    '''
    Setup slave node.
    - Allow connections from masters
    - Set hostname
    - Write /etc/hosts
    - Write /etc/splice-testing.yaml
    '''
    try:
        client.run_sync("touch /tmp/hosts")
        sftp.put(hostsfile, "/tmp/hosts")
        client.run_sync("cat /etc/hosts >> /tmp/hosts")
        client.run_sync("sort -u /tmp/hosts > /etc/hosts")
        sftp.put(yamlfile, "/etc/splice-testing.yaml")
        client.run_sync("touch /etc/splice-testing.yaml")
        if hostname:
            client.run_sync("hostname " + hostname)
            client.run_sync("sed -i 's,^HOSTNAME=.*$,HOSTNAME=" + hostname + ",' /etc/sysconfig/network")
        for key in master_keys:
            if key:
                client.run_sync("cat /root/.ssh/authorized_keys > /root/.ssh/authorized_keys.new")
                client.run_sync("echo '" + key + "' >> /root/.ssh/authorized_keys.new")
                client.run_sync("sort -u /root/.ssh/authorized_keys.new | grep -v '^$' > /root/.ssh/authorized_keys")
        if setup_script:
            sftp.put(setup_script, "/root/instance_setup_script")
            client.run_sync("chmod 755 /root/instance_setup_script")
            client.run_sync("/root/instance_setup_script")
    except Exception, e:
        logging.error('Caught exception in setup_slave: ' + str(e.__class__) + ': ' + str(e))


argparser = argparse.ArgumentParser(description='Create CloudFormation stack and run the testing')
argparser.add_argument('--rhuirhelversion', help='RHEL version for RHUI setup (RHEL63, RHEL64)', default="RHEL64")

argparser.add_argument('--rhel5', help='number of RHEL5 clients', type=int, default=0)
argparser.add_argument('--rhel6', help='number of RHEL6 clients', type=int, default=0)
argparser.add_argument('--cds', help='number of CDSes instances', type=int, default=0)
argparser.add_argument('--proxy', help='create RHUA<->CDN proxy', action='store_true')
argparser.add_argument('--rhua', help='create RHUA node', action='store_true')
argparser.add_argument('--satellite', help='create Satellite node', action='store_true')
argparser.add_argument('--sam', help='create Sam node', action='store_true')
argparser.add_argument('--config',
                       default="/etc/validation.yaml", help='use supplied yaml config file')
argparser.add_argument('--debug', action='store_const', const=True,
                       default=False, help='debug mode')
argparser.add_argument('--fakecf', action='store_const', const=True,
                       default=False, help='use fakecf creator')
argparser.add_argument('--dry-run', action='store_const', const=True,
                       default=False, help='do not run stack creation, validate only')
argparser.add_argument('--parameters', metavar='<expr>', nargs="*",
                       help="space-separated NAME=VALUE list of parametars")
argparser.add_argument('--region',
                       default="us-east-1", help='use specified region')
argparser.add_argument('--timeout', type=int,
                       default=10, help='stack creation timeout')

argparser.add_argument('--vpcid', help='VPCid')
argparser.add_argument('--subnetid', help='Subnet id (for VPC)')

argparser.add_argument('--instancesetup', help='Instance setup script for all instances except master node')
argparser.add_argument('--mastersetup', help='Instance setup script for master node')

args = argparser.parse_args()

if args.debug:
    loglevel = logging.DEBUG
else:
    loglevel = logging.INFO

REGION = args.region

logging.basicConfig(level=loglevel, format='%(asctime)s %(levelname)s %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')

if args.debug:
    logging.getLogger("paramiko").setLevel(logging.DEBUG)
else:
    logging.getLogger("paramiko").setLevel(logging.WARNING)

if (args.vpcid and not args.subnetid) or (args.subnetid and not args.vpcid):
    logging.error("vpcid and subnetid parameters should be set together!")
    sys.exit(1)

if args.instancesetup:
    fd = open(args.instancesetup, 'r')
    instancesetup = fd.read()
    fd.close()

if args.mastersetup:
    fd = open(args.mastersetup, 'r')
    mastersetup = fd.read()
    fd.close()

try:
    with open(args.config, 'r') as confd:
        valid_config = yaml.load(confd)

    (ssh_key_name, ssh_key) = valid_config["ssh"][REGION]
    ec2_key = valid_config["ec2"]["ec2-key"]
    ec2_secret_key = valid_config["ec2"]["ec2-secret-key"]

except Exception as e:
    logging.error("got '%s' error processing: %s" % (e, args.config))
    logging.error("Please, check your config or and try again")
    sys.exit(1)

json_dict = {}

json_dict['AWSTemplateFormatVersion'] = '2010-09-09'

json_dict['Description'] = 'DOMAIN with %s CDSes' % args.cds
if args.rhel5 > 0:
    json_dict['Description'] += ", %s RHEL5 clients" % args.rhel5
if args.rhel6 > 0:
    json_dict['Description'] += ", %s RHEL6 clients" % args.rhel6
if args.proxy:
    json_dict['Description'] += ", A PROXY"
if args.satellite:
    json_dict['Description'] += ", A SATELLITE"
if args.sam:
    json_dict['Description'] += ", A SAM"

json_dict['Description'] += " AND WITH"
if not args.rhua:
    json_dict['Description'] += "OUT"
json_dict['Description'] += " RHUA."


json_dict['Mappings'] = \
  {u'F18': {u'ap-northeast-1': {u'AMI': u'ami-33b23b32'},
                u'ap-southeast-1': {u'AMI': u'ami-4c327c1e'},
                u'ap-southeast-2': {u'AMI': u'ami-33d24109'},
                u'eu-west-1': {u'AMI': u'ami-43809137'},
                u'sa-east-1': {u'AMI': u'ami-08eb4e15'},
                u'us-east-1': {u'AMI': u'ami-b71078de'},
                u'us-west-1': {u'AMI': u'ami-674f6122'},
                u'us-west-2': {u'AMI': u'ami-fd9302cd'}},
   u'F19': {u'ap-northeast-1': {u'AMI': u'ami-95b52094'},
                u'ap-southeast-1': {u'AMI': u'ami-da450c88'},
                u'ap-southeast-2': {u'AMI': u'ami-5565f66f'},
                u'eu-west-1': {u'AMI': u'ami-f1031e85'},
                u'sa-east-1': {u'AMI': u'ami-b055f0ad'},
                u'us-east-1': {u'AMI': u'ami-b22e5cdb'},
                u'us-west-1': {u'AMI': u'ami-10cce555'},
                u'us-west-2': {u'AMI': u'ami-9727b7a7'}},
   u'RHEL58': {u'ap-northeast-1': {u'AMI': u'ami-60229461'},
                u'ap-southeast-1': {u'AMI': u'ami-da8dc988'},
                u'ap-southeast-2': {u'AMI': u'ami-65b7205f'},
                u'eu-west-1': {u'AMI': u'ami-47615833'},
                u'sa-east-1': {u'AMI': u'ami-f46cb3e9'},
                u'us-east-1': {u'AMI': u'ami-fb0ddc92'},
                u'us-west-1': {u'AMI': u'ami-c5bde480'},
                u'us-west-2': {u'AMI': u'ami-3e0a870e'}},
   u'RHEL59': {u'ap-northeast-1': {u'AMI': u'ami-397cc638'},
                u'ap-southeast-1': {u'AMI': u'ami-7486c426'},
                u'ap-southeast-2': {u'AMI': u'ami-33d14709'},
                u'eu-west-1': {u'AMI': u'ami-c00906b4'},
                u'sa-east-1': {u'AMI': u'ami-412bf35c'},
                u'us-east-1': {u'AMI': u'ami-4f53dd26'},
                u'us-west-1': {u'AMI': u'ami-74d7f731'},
                u'us-west-2': {u'AMI': u'ami-72b93242'}},
   u'RHEL63': {u'ap-northeast-1': {u'AMI': u'ami-5453e055'},
                u'ap-southeast-1': {u'AMI': u'ami-24e5a376'},
                u'ap-southeast-2': {u'AMI': u'ami-8f8413b5'},
                u'eu-west-1': {u'AMI': u'ami-8bf2f7ff'},
                u'sa-east-1': {u'AMI': u'ami-4807d955'},
                u'us-east-1': {u'AMI': u'ami-cc5af9a5'},
                u'us-west-1': {u'AMI': u'ami-51f4ae14'},
                u'us-west-2': {u'AMI': u'ami-8a25a9ba'}},
   u'RHEL64': {u'ap-northeast-1': {u'AMI': u'ami-8f11958e'},
                u'ap-southeast-1': {u'AMI': u'ami-3a367b68'},
                u'ap-southeast-2': {u'AMI': u'ami-8c1f89b6'},
                u'eu-west-1': {u'AMI': u'ami-22c8c156'},
                u'sa-east-1': {u'AMI': u'ami-8bb76c96'},
                u'us-east-1': {u'AMI': u'ami-d94bdcb0'},
                u'us-west-1': {u'AMI': u'ami-fc9cbfb9'},
                u'us-west-2': {u'AMI': u'ami-5e57dd6e'}}
   }

json_dict['Parameters'] = \
{u'KeyName': {u'Description': u'Name of an existing EC2 KeyPair to enable SSH access to the instances',
              u'Type': u'String'}}

json_dict['Resources'] = \
{u'CLIsecuritygroup': {u'Properties': {u'GroupDescription': u'CLI security group',
                                       u'SecurityGroupIngress': [{u'CidrIp': u'0.0.0.0/0',
                                                                  u'FromPort': u'22',
                                                                  u'IpProtocol': u'tcp',
                                                                  u'ToPort': u'22'}]},
                       u'Type': u'AWS::EC2::SecurityGroup'},
 u'MASTERsecuritygroup': {u'Properties': {u'GroupDescription': u'MASTER security group',
                                          u'SecurityGroupIngress': [{u'CidrIp': u'0.0.0.0/0',
                                                                     u'FromPort': u'22',
                                                                     u'IpProtocol': u'tcp',
                                                                     u'ToPort': u'22'},
                                                                    {u'CidrIp': u'0.0.0.0/0',
                                                                     u'FromPort': u'27017',
                                                                     u'IpProtocol': u'tcp',
                                                                     u'ToPort': u'27017'}]},
                          u'Type': u'AWS::EC2::SecurityGroup'},
 u'PROXYsecuritygroup': {u'Properties': {u'GroupDescription': u'PROXY security group',
                                         u'SecurityGroupIngress': [{u'CidrIp': u'0.0.0.0/0',
                                                                    u'FromPort': u'22',
                                                                    u'IpProtocol': u'tcp',
                                                                    u'ToPort': u'22'},
                                                                   {u'CidrIp': u'0.0.0.0/0',
                                                                    u'FromPort': u'3128',
                                                                    u'IpProtocol': u'tcp',
                                                                    u'ToPort': u'3128'}]},
                         u'Type': u'AWS::EC2::SecurityGroup'},
 u'RHUIsecuritygroup': {u'Properties': {u'GroupDescription': u'RHUI security group',
                                        u'SecurityGroupIngress': [{u'CidrIp': u'0.0.0.0/0',
                                                                   u'FromPort': u'22',
                                                                   u'IpProtocol': u'tcp',
                                                                   u'ToPort': u'22'},
                                                                  {u'CidrIp': u'0.0.0.0/0',
                                                                   u'FromPort': u'443',
                                                                   u'IpProtocol': u'tcp',
                                                                   u'ToPort': u'443'},
                                                                  {u'CidrIp': u'0.0.0.0/0',
                                                                   u'FromPort': u'5674',
                                                                   u'IpProtocol': u'tcp',
                                                                   u'ToPort': u'5674'}]},
                        u'Type': u'AWS::EC2::SecurityGroup'},
 u'SAMsecuritygroup': {u'Properties': {u'GroupDescription': u'Sam security group',
                                        u'SecurityGroupIngress': [{u'CidrIp': u'0.0.0.0/0',
                                                                   u'FromPort': u'22',
                                                                   u'IpProtocol': u'tcp',
                                                                   u'ToPort': u'22'},
                                                                  {u'CidrIp': u'0.0.0.0/0',
                                                                   u'FromPort': u'443',
                                                                   u'IpProtocol': u'tcp',
                                                                   u'ToPort': u'443'},
                                                                  {u'CidrIp': u'0.0.0.0/0',
                                                                   u'FromPort': u'8088',
                                                                   u'IpProtocol': u'tcp',
                                                                   u'ToPort': u'8088'}]},
                        u'Type': u'AWS::EC2::SecurityGroup'},
 u'SATELLITEsecuritygroup': {u'Properties': {u'GroupDescription': u'Satellite security group',
                                        u'SecurityGroupIngress': [{u'CidrIp': u'0.0.0.0/0',
                                                                   u'FromPort': u'22',
                                                                   u'IpProtocol': u'tcp',
                                                                   u'ToPort': u'22'},
                                                                  {u'CidrIp': u'0.0.0.0/0',
                                                                   u'FromPort': u'443',
                                                                   u'IpProtocol': u'tcp',
                                                                   u'ToPort': u'443'},
                                                                  {u'CidrIp': u'0.0.0.0/0',
                                                                   u'FromPort': u'80',
                                                                   u'IpProtocol': u'tcp',
                                                                   u'ToPort': u'80'}]},
                        u'Type': u'AWS::EC2::SecurityGroup'}}


json_dict['Resources']["master"] = \
{u'Properties': {u'ImageId': {u'Fn::FindInMap': [u'F19',
                                                             {u'Ref': u'AWS::Region'},
                                                             u'AMI']},
                             u'InstanceType': u'c1.medium',
                             u'KeyName': {u'Ref': u'KeyName'},
                             u'SecurityGroups': [{u'Ref': u'MASTERsecuritygroup'}],
                             u'BlockDeviceMappings': [
                                    {
                                        u"DeviceName" : u"/dev/sda1",
                                        u"Ebs" : { u"VolumeSize" : u"10" }
                                    }
                               ],
                             u'Tags': [{u'Key': u'Name',
                                        u'Value': {u'Fn::Join': [u'_',
                                                                 [u'RHUI_Master',
                                                                  {u'Ref': u'KeyName'}]]}},
                                       {u'Key': u'Role',
                                        u'Value': u'Master'},
                                       {u'Key': u'PrivateHostname',
                                        u'Value': u'master.example.com'},
                                       {u'Key': u'PublicHostname',
                                        u'Value': u'master_pub.example.com'}]},
             u'Type': u'AWS::EC2::Instance'}
if args.rhua:
    json_dict['Resources']["rhua"] = \
    {u'Properties': {u'ImageId': {u'Fn::FindInMap': [args.rhuirhelversion,
                                                               {u'Ref': u'AWS::Region'},
                                                               u'AMI']},
                               u'InstanceType': u'm1.large',
                               u'KeyName': {u'Ref': u'KeyName'},
                               u'SecurityGroups': [{u'Ref': u'RHUIsecuritygroup'}],
                               u'Tags': [{u'Key': u'Name',
                                          u'Value': {u'Fn::Join': [u'_',
                                                                   [u'RHUA',
                                                                    {u'Ref': u'KeyName'}]]}},
                                         {u'Key': u'Role', u'Value': u'RHUA'},
                                         {u'Key': u'PrivateHostname',
                                          u'Value': u'rhua.example.com'},
                                         {u'Key': u'PublicHostname',
                                          u'Value': u'rhua_pub.example.com'}]},
               u'Type': u'AWS::EC2::Instance'}
if args.satellite:
     json_dict['Resources']["satellite"] = \
     {u'Properties': {u'ImageId': {u'Fn::FindInMap': [args.rhuirhelversion,
                                                               {u'Ref': u'AWS::Region'},
                                                               u'AMI']},
                               u'InstanceType': u'm1.large',
                               u'KeyName': {u'Ref': u'KeyName'},
                               u'SecurityGroups': [{u'Ref': u'SATELLITEsecuritygroup'}],
                               u'BlockDeviceMappings': [
                                    {
                                        u"DeviceName" : u"/dev/sda1",
                                        u"Ebs" : { u"VolumeSize" : u"30" }
                                    }
                               ],
                               u'Tags': [{u'Key': u'Name',
                                          u'Value': {u'Fn::Join': [u'_',
                                                                   [u'SATELLITE',
                                                                    {u'Ref': u'KeyName'}]]}},
                                         {u'Key': u'Role', u'Value': u'SATELLITE'},
                                         {u'Key': u'PrivateHostname',
                                          u'Value': u'satellite.example.com'},
                                         {u'Key': u'PublicHostname',
                                          u'Value': u'satellite_pub.example.com'}]},
               u'Type': u'AWS::EC2::Instance'}

if args.sam:
     json_dict['Resources']["sam"] = \
     {u'Properties': {u'ImageId': {u'Fn::FindInMap': [args.rhuirhelversion,
                                                               {u'Ref': u'AWS::Region'},
                                                               u'AMI']},
                               u'InstanceType': u'm1.large',
                               u'KeyName': {u'Ref': u'KeyName'},
                               u'SecurityGroups': [{u'Ref': u'SAMsecuritygroup'}],
                               u'BlockDeviceMappings': [
                                    {
                                        u"DeviceName" : u"/dev/sda1",
                                        u"Ebs" : { u"VolumeSize" : u"30" }
                                    }
                               ],
                               u'Tags': [{u'Key': u'Name',
                                          u'Value': {u'Fn::Join': [u'_',
                                                                   [u'SAM',
                                                                    {u'Ref': u'KeyName'}]]}},
                                         {u'Key': u'Role', u'Value': u'SAM'},
                                         {u'Key': u'PrivateHostname',
                                          u'Value': u'sam.example.com'},
                                         {u'Key': u'PublicHostname',
                                          u'Value': u'sam_pub.example.com'}]},
               u'Type': u'AWS::EC2::Instance'}


if args.proxy:
    json_dict['Resources']["proxy"] = \
     {u'Properties': {u'ImageId': {u'Fn::FindInMap': [u"RHEL64",
                                                            {u'Ref': u'AWS::Region'},
                                                            u'AMI']},
                            u'InstanceType': u'm1.small',
                            u'KeyName': {u'Ref': u'KeyName'},
                            u'SecurityGroups': [{u'Ref': u'PROXYsecuritygroup'}],
                            u'Tags': [{u'Key': u'Name',
                                       u'Value': {u'Fn::Join': [u'_',
                                                                [u'PROXY',
                                                                 {u'Ref': u'KeyName'}]]}},
                                      {u'Key': u'Role', u'Value': u'PROXY'},
                                      {u'Key': u'PrivateHostname',
                                       u'Value': u'proxy.example.com'},
                                      {u'Key': u'PublicHostname',
                                       u'Value': u'proxy_pub.example.com'}]},
            u'Type': u'AWS::EC2::Instance'}

for i in range(1, args.cds + 1):
    json_dict['Resources']["cds%i" % i] = \
        {u'Properties': {u'ImageId': {u'Fn::FindInMap': [args.rhuirhelversion,
                                                           {u'Ref': u'AWS::Region'},
                                                           u'AMI']},
                           u'InstanceType': u'm1.large',
                           u'KeyName': {u'Ref': u'KeyName'},
                           u'SecurityGroups': [{u'Ref': u'RHUIsecuritygroup'}],
                           u'Tags': [{u'Key': u'Name',
                                      u'Value': {u'Fn::Join': [u'_',
                                                               [u'CDS%i' %i,
                                                                {u'Ref': u'KeyName'}]]}},
                                     {u'Key': u'Role', u'Value': u'CDS'},
                                     {u'Key': u'PrivateHostname',
                                      u'Value': u'cds%i.example.com' % i},
                                     {u'Key': u'PublicHostname',
                                      u'Value': u'cds%i_pub.example.com' % i}]},
           u'Type': u'AWS::EC2::Instance'}

for i in range(1, args.rhel5 + args.rhel6 + 1):
    if i > args.rhel5:
        os = "RHEL64"
    else:
        os = "RHEL59"
    json_dict['Resources']["cli%i" % i] = \
        {u'Properties': {u'ImageId': {u'Fn::FindInMap': [os,
                                                           {u'Ref': u'AWS::Region'},
                                                           u'AMI']},
                           u'InstanceType': u'm1.small',
                           u'KeyName': {u'Ref': u'KeyName'},
                           u'SecurityGroups': [{u'Ref': u'CLIsecuritygroup'}],
                           u'Tags': [{u'Key': u'Name',
                                      u'Value': {u'Fn::Join': [u'_',
                                                               [u'RHUI_CLI%i' % i,
                                                                {u'Ref': u'KeyName'}]]}},
                                     {u'Key': u'Role', u'Value': u'CLI'},
                                     {u'Key': u'PrivateHostname',
                                      u'Value': u'cli%i.example.com' % i},
                                     {u'Key': u'PublicHostname',
                                      u'Value': u'cli%i_pub.example.com' % i},
                                     {u'Key': u'OS', u'Value': u'%s' % os[:5]}]},
           u'Type': u'AWS::EC2::Instance'}

if args.vpcid and args.subnetid:
    # Setting VpcId and SubnetId
    json_dict['Outputs'] = {}
    for key in json_dict['Resources'].keys():
        # We'll be changing dictionary so .keys() is required here!
        if json_dict['Resources'][key]['Type'] == 'AWS::EC2::SecurityGroup':
            json_dict['Resources'][key]['Properties']['VpcId'] = args.vpcid
        elif json_dict['Resources'][key]['Type'] == 'AWS::EC2::Instance':
            json_dict['Resources'][key]['Properties']['SubnetId'] = args.subnetid
            json_dict['Resources'][key]['Properties']['SecurityGroupIds'] = json_dict['Resources'][key]['Properties'].pop('SecurityGroups')
            json_dict['Resources']["%sEIP" % key] = \
            {
                "Type" : "AWS::EC2::EIP",
                "Properties" : {"Domain" : "vpc",
                                "InstanceId" : { "Ref" : key }
                               }
            }
else:
#    json_dict['Outputs'] = \
#    {u'IPMaster': {u'Description': u'master.example.com IP',
#               u'Value': {u'Fn::GetAtt': [u'master', u'PublicIP']}}}
    pass
json_dict['Outputs'] = {}

json_body =  json.dumps(json_dict, indent=4)

region = regioninfo.RegionInfo(name=args.region,
                               endpoint="cloudformation." + args.region + ".amazonaws.com")

if not region:
    logging.error("Unable to connect to region: " + args.region)
    sys.exit(1)

if args.fakecf:
    from fakecf.fakecf import *
    con_cf = FakeCF(aws_access_key_id=ec2_key,
                    aws_secret_access_key=ec2_secret_key,
                    region=args.region)
else:
    con_cf = cloudformation.connection.CloudFormationConnection(aws_access_key_id=ec2_key,
                                                                aws_secret_access_key=ec2_secret_key,
                                                                region=region)

con_ec2 = ec2.connect_to_region(args.region,
                                aws_access_key_id=ec2_key,
                                aws_secret_access_key=ec2_secret_key)

if not con_cf or not con_ec2:
    logging.error("Create CF/EC2 connections: " + args.region)
    sys.exit(1)

STACK_ID = "STACK" + ''.join(random.choice(string.ascii_lowercase) for x in range(10))
logging.info("Creating stack with ID " + STACK_ID)

parameters = []
try:
    if args.parameters:
        for param in args.parameters:
            parameters.append(tuple(param.split('=')))
except:
    logging.error("Wrong parameters format")
    sys.exit(1)

parameters.append(("KeyName", ssh_key_name))

if args.dry_run:
    sys.exit(0)

con_cf.create_stack(STACK_ID, template_body=json_body,
                    parameters=parameters, timeout_in_minutes=args.timeout)

is_complete = False
result = False
while not is_complete:
    time.sleep(10)
    try:
        for event in con_cf.describe_stack_events(STACK_ID):
            if event.resource_type == "AWS::CloudFormation::Stack" and event.resource_status == "CREATE_COMPLETE":
                logging.info("Stack creation completed")
                is_complete = True
                result = True
                break
            if event.resource_type == "AWS::CloudFormation::Stack" and event.resource_status == "ROLLBACK_COMPLETE":
                logging.info("Stack creation failed")
                is_complete = True
                break
    except:
        # Sometimes 'Rate exceeded' happens
        pass

if not result:
    sys.exit(1)

instances = []
for res in con_cf.describe_stack_resources(STACK_ID):
    # we do care about instances only
    if res.resource_type == 'AWS::EC2::Instance' and res.physical_resource_id:
        logging.debug("Instance " + res.physical_resource_id + " created")
        instances.append(res.physical_resource_id)

instances_detail = []
hostsfile = tempfile.NamedTemporaryFile(delete=False)
logging.debug("Created temporary file for /etc/hosts " + hostsfile.name)
yamlfile = tempfile.NamedTemporaryFile(delete=False)
logging.debug("Created temporary YAML config " + yamlfile.name)
for i in con_ec2.get_all_instances():
    for ii in  i.instances:
        if ii.id in instances:
            try:
                public_hostname = ii.tags["PublicHostname"]
            except KeyError:
                public_hostname = None
            try:
                private_hostname = ii.tags["PrivateHostname"]
            except KeyError:
                private_hostname = None
            try:
                role = ii.tags["Role"]
            except KeyError:
                role = None

            if ii.ip_address:
                public_ip = ii.ip_address
            else:
                public_ip = ii.private_ip_address
            private_ip = ii.private_ip_address

            details_dict = {"id": ii.id,
                            "public_hostname": public_hostname,
                            "private_hostname": private_hostname,
                            "role": role,
                            "public_ip": public_ip,
                            "private_ip": private_ip,
                            "public_dns_name": ii.public_dns_name}

            for tag_key in ii.tags.keys():
                if tag_key not in ["PublicHostname", "PrivateHostname", "Role"]:
                    details_dict[tag_key] = ii.tags[tag_key]

            instances_detail.append(details_dict)

            if private_hostname and private_ip:
                hostsfile.write(private_ip + "\t" + private_hostname + "\n")
            if public_hostname and public_ip:
                hostsfile.write(public_ip + "\t" + public_hostname + "\n")
            if private_ip and 'public_dns_name' in details_dict:
                hostsfile.write(private_ip + "\t" + details_dict['public_dns_name'] + "\n")

yamlconfig = {'Instances': instances_detail[:]}
yamlfile.write(yaml.safe_dump(yamlconfig))
yamlfile.close()
hostsfile.close()
logging.debug(instances_detail)
master_keys = []
for instance in instances_detail:
    if instance["public_ip"]:
        ip = instance["public_ip"]
        logging.info("Instance with public ip created: " + instance["role"] + ":" + instance["public_hostname"] + ":" + ip)
    else:
        ip = instance["private_ip"]
        logging.info("Instance with private ip created: " + instance["role"] + ":" + instance["private_hostname"] + ":" + ip)
    (instance["client"], instance["sftp"]) = setup_host_ssh(ip, ssh_key)
    if instance["role"] == "Master":
        master_keys.append(setup_master(instance["client"]))

for instance in instances_detail:
    if instance["private_hostname"]:
        hostname = instance["private_hostname"]
    else:
        hostname = instance["public_hostname"]
    setup_script = None
    if instance["role"] == "Master" and args.mastersetup:
        setup_script = args.mastersetup
    if instance["role"] != "Master" and args.instancesetup:
        setup_script = args.instancesetup

    if instance["role"].upper() == 'SAM':
        setup_slave(instance["client"], instance["sftp"], instance['public_dns_name'],
                    hostsfile.name, yamlfile.name, master_keys, setup_script)
        continue

    setup_slave(instance["client"], instance["sftp"], hostname,
                hostsfile.name, yamlfile.name, master_keys, setup_script)
