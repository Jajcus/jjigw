#!/usr/bin/python

""" Simple Programmable Ident (RFC1413) Daemon """

import sys
import signal
import socket
import select
import threading
import os
import errno
import pwd
import grp
import getopt

class Driver:
	def __init__(self):
		pass
	def lookup(self,localport,localip,remoteport,remoteip):
		return None

class NoUserDriver(Driver):
	def lookup(self,localport,localip,remoteport,remoteip):
		return "ERROR:NO-USER"

class HiddenUserDriver(Driver):
	def lookup(self,localport,localip,remoteport,remoteip):
		return "ERROR:HIDDEN-USER"

class FailDriver(Driver):
	def lookup(self,localport,localip,remoteport,remoteip):
		return "ERROR:UNKNOWN-ERROR"

class StaticDriver(Driver):
	def __init__(self,reply):
		self.reply=reply
	def lookup(self,localport,localip,remoteport,remoteip):
		return self.reply

class RealDriver(Driver):
	def lookup(self,localport,localip,remoteport,remoteip):
		try:
			f=file("/proc/net/tcp","r")
		except IOError:
			print >>sys.stderr,"Couldn't open /proc/net/tcp"
			return None
		f.readline()
		li=[long(i) for i in localip.split(".")]
		localip=(li[0]<<24)+(li[1]<<16)+(li[2]<<8)+li[3]
		ri=[long(i) for i in remoteip.split(".")]
		remoteip=(ri[0]<<24)+(ri[1]<<16)+(ri[2]<<8)+ri[3]
		for l in f.xreadlines():
			sp=l.split()
			if len(sp)<8:
				continue
			locip,locport=sp[1].split(":")
			locip=socket.htonl(long(locip,16))&0xffffffffL
			locport=int(locport,16)
			remip,remport=sp[2].split(":")
			remip=socket.htonl(long(remip,16))&0xffffffffL
			remport=int(remport,16)
			print "%r:%r,%r:%r vs %r:%r,%r:%r" % (
				localip,localport,remoteip,remoteport,
				locip,locport,remip,remport)
			if (localip,localport,remoteip,remoteport)!=(locip,locport,remip,remport):
				continue
			uid=int(sp[7])
			try:
				pw=pwd.getpwuid(uid)
				return pw[0]
			except:
				return "ERROR:NO-USER"
		return None	

class SocketDriverClient:
	def __init_(self,driver,sock):
		self.socket=sock
		self.thread=threading.Thread(target=self.run_thread)
		self.thread.setDaemon(1)
		self.conn_users={}
		self.thread.run()

class SocketDriver(Driver):
	def __init__(self,path):
		self.socket=socket.socket(socket.AF_UNIX)
		self.socket.bind(path)
		self.socket.listen(1)
		self.clients=[]
		register_listening_socket(self.socket,self.accept_connection)
	def lookup(self,localport,localip,remoteport,remoteip):
		for c in self.clients:
			connuser=c.conn_users.get[(localport,localip,remoteport,remoteip)]
			if connuser:
				return connuser
		return None
	def accept_connection(self,sock):
		sock,addr=self.socket.accept()
		print >>sys.stderr,"Client connection from: %r" % (addr,)
		self.clients.append(SocketDriverClient(self,sock))
	
listening_socket_handlers={}
def register_listening_socket(sock,handler):
	global listening_sockets
	listening_socket_handlers[sock]=handler

def input_thread(sock,addr):
	print >>sys.stderr,"Connection from: %r" % (addr,)
	localip=sock.getsockname()[0]
	remoteip=addr[0]
	buf=""
	while 1:
		try:
			r=sock.recv(1024)
		except socket.error,e:
			if e.args[0]==errno.EINTR:
				continue
		if not r:
			print >>sys.stderr,"No query"
			sock.close()
			return
		buf+=r
		if len(buf)>1024:
			print >>sys.stderr,"Query too long"
			sock.close()
			return
		if buf.find("\r\n"):
			break
	query=buf.split("\r",1)[0]
	print >>sys.stderr,"Query: %r" % (query,)
	sp=query.split(",")
	if len(sp)!=2:
		print >>sys.stderr,"Invalid query"
		sock.close()
		return
	local,remote=sp
	try:
		local=int(local.strip())
		remote=int(remote.strip())
	except ValueError:
		sock.send("%s,%s:ERROR:INVALID-PORT\r\n" % (local,remote))
		sock.close()
		print >>sys.stderr,"Invalid query"
		return
	if local<1 or local>65535 or remote<1 or remote>65535:
		sock.send("%i,%i:ERROR:INVALID-PORT\r\n" % (local,remote))
		sock.close()
		print >>sys.stderr,"Invalid query"
		return
	reply=None
	for d in drivers:
		r=d.lookup(local,localip,remote,remoteip)
		print `r`
		if r is None:
			continue
		elif r.startswith("ERROR:"):
			reply="%i,%i:%s" % (local,remote,r)
			break
		else:
			reply="%i,%i:USERID:UNIX:%s" % (local,remote,r)
			break
	if not reply:
		reply="%i,%i:ERROR:NO-USER" % (local,remote)
	print >>sys.stderr,"Reply: %s" % (reply,)
	sock.send(reply+"\r\n")
	sock.close()
	return

def signal_handler(signum,frame):
	global exit
	exit=1
	print >>sys.stderr,"Signal %i received, exiting." % (signum,)


def accept_connection(sock):
	th=threading.Thread(target=input_thread,args=sock.accept())
	th.setDaemon(1)
	th.start()

def usage():
	print "Simple Programmable Ident (RFC1413) Daemon"
	print "(c) 2004 Jacek Konieczny"
	print
	print "Usage:"
	print "    %s [options] [driver...]"
	print 
	print "Options:"
	print "    -h --help              display this help and exit."
	print "    -i ADDR --ip=ADDR      bind to IP address ADDR."
	print "    -p PORT --port=PORT    bind to port PORT (default: 113)."
	print "    -p USER --user=USER    when started with uid=0, switch "
	print "                           to user USER (default: 'nobody')"
	print "    -p GROUP --group=GROUP when started with uid=0, switch "
	print "                           to group GROUP (default: nobody's group)"
	print "Drivers:"
	print "    --nouser               always reply with NO-USER error."
	print "    --hidden               always reply with HIDDN-USER error."
	print "    --fail                 always reply with UNKNOWN-ERROR error."
	print "    --real                 reply with real connection user name (currently Linux only)."
	print "    --static=USER          always reply with the same USER reply."
	print "    --socket=PATH          listen on UNIX socket PATH for other servers"
	print "                           registering their connections."


user="nobody"
group=None
ip="0.0.0.0"
port=113
drivers=[]

try:
	opts,args=getopt.getopt(sys.argv[1:], "hi:p:u:g:", ["help","ip=","port=","user=","group=",
		"nouser","hidden","real","fail","static=","socket="])
except:
	usage()
	sys.exit(2)
for o,a in opts:
	if o in ("-h","--help"):
		usage()
		sys.exit()
	if o in ("-i","--ip"):
		ip=a
	if o in ("-p","--port"):
		port=int(a)
	if o in ("-u","--user"):
		user=a
	if o in ("-g","--group"):
		group=a
	if o in ("--nouser",):
		drivers.append(NoUserDriver())
	if o in ("--hidden",):
		drivers.append(HiddenUserDriver())
	if o in ("--fail",):
		drivers.append(FailDriver())
	if o in ("--real",):
		drivers.append(RealDriver())
	if o in ("--static",):
		drivers.append(StaticDriver(a))
	if o in ("--socket",):
		drivers.append(SocketDriver(a))
drivers.append(FailDriver())
	
if args:
	usage()
	sys.exit(2)

sock=socket.socket()
sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
sock.bind((ip,port))

if os.getuid()==0:
	pw=pwd.getpwnam(user)
	uid=pw[2]
	gid=pw[3]
	if group:
		gr=grp.getgrnam(group)
		gid=gr[2]
	os.setgroups([gid])
	os.setgid(gid)
	os.setuid(uid)

sock.listen(1)
buf=""
exit=0

register_listening_socket(sock,accept_connection)

signal.signal(signal.SIGINT,signal_handler)
signal.signal(signal.SIGPIPE,signal_handler)
signal.signal(signal.SIGTERM,signal_handler)

while not exit and listening_socket_handlers:
	try:
		sockets=listening_socket_handlers.keys()
		id,od,ed=select.select(sockets,[],sockets,1)
	except select.error,e:
		if e.args[0]==errno.EINTR:
			continue
	for s in id:
		listening_socket_handlers[s](s)
	for s in ed:
		print >>sys.stderr,"Error on socket: %r" % (s,)
		del listening_socket_handlers[s]
