#! /bin/bash

# logfile
exec &> /var/log/katello-nightly-setup.log 2>&1

# set up iptables
iptables -I INPUT -p tcp --destination-port 80 -j ACCEPT
iptables -I INPUT -p tcp --destination-port 443 -j ACCEPT
iptables -I INPUT -p tcp --destination-port 8088 -j ACCEPT
service iptables save

# hostname
hostname `curl http://169.254.169.254/latest/meta-data/public-hostname`
sed -i s,HOSTNAME=.*$,HOSTNAME=`hostname`, /etc/sysconfig/network

# repos
rpm -Uvh http://dl.fedoraproject.org/pub/epel/6/x86_64/epel-release-6-8.noarch.rpm
rpm -Uvh http://fedorapeople.org/groups/katello/releases/yum/nightly/RHEL/6/x86_64/katello-repos-latest.rpm

# install katello-headpin
yum install -y katello-headpin-all

# setenforce 0
setenforce 0
sed -i s,SELINUX=enforcing,SELINUX=permissive, /etc/sysconfig/selinux

# configure katello in sam mode
katello-configure --no-bars --reset-data=YES --deployment=sam --user-pass=admin

# ensure katello is up and running
katello-service start

cat /home/ec2-user/.ssh/authorized_keys > /root/.ssh/authorized_keys
