#!/usr/bin/python -u

import sys
import libxml2
import threading
import re
import select
import socket
import user
import sha
import string

from pyxmpp import ClientStream,JID,Iq,Presence,Message,StreamError
import pyxmpp.jabberd
from pyxmpp.jabber.muc import MucPresence,MucX,MucUserX,MucItem,MUC_NS

class ConnectConfig:
    def __init__(self,node):
	self.node=node
	self.host=node.xpathEval("host")[0].getContent()
	self.port=int(node.xpathEval("port")[0].getContent())
	self.secret=node.xpathEval("secret")[0].getContent()

class NetworkConfig:
    def __init__(self,node):
	self.node=node
	self.jid=JID(node.prop("jid"))
	servers=node.xpathEval("server")
	self.servers=[]
	for s in servers:
	    self.servers.append((s.getContent(),6667))
	self.default_encoding="iso-8859-2"
    def get_servers(self):
	r=self.servers
	self.servers=self.servers[-1:]+self.servers[1:]
	return r

class Config:
    def __init__(self,filename):
	self.doc=libxml2.parseFile(filename)
	self.connect=ConnectConfig(self.doc.xpathEval("jjit/connect")[0])
	self.network=NetworkConfig(self.doc.xpathEval("jjit/network")[0])
    def __del__(self):
	self.doc.freeDoc()


evil_characters_re=re.compile(r"[\000-\010\013\014\016-\037]")

def remove_evil_characters(s):
    return evil_characters_re.sub(" ",s)

channel_re=re.compile(r"^[&#+!][^\000 \007 ,:\r\n]{1,49}$")
nick_re=re.compile(r"^[a-zA-Z\x5b-\x60\x7b-\x7d\[\]\\`_^{|}][a-zA-Z\x5b-\x60\x7b-\x7d\[\]\\`_^{|}0-9-]{0,8}$")

def escape_node_string(s):
    s=s.replace(",quot,",'"')
    s=s.replace(",amp,","&")
    s=s.replace(",apos,","'")
    s=s.replace(",slash,","/")
    s=s.replace(",lt,","<")
    s=s.replace(",gt,",">")
    s=s.replace(",at,","@")
    return s

def unescape_node_string(s):
    s=s.replace('"',",quot,")
    s=s.replace("&",",amp,")
    s=s.replace("'",",apos,")
    s=s.replace("/",",slash,")
    s=s.replace("<",",lt,")
    s=s.replace(">",",gt,")
    s=s.replace("@",",at,")
    return s

def node_to_channel(n,encoding):
    s=n.encode(encoding,"strict")
    s=escape_node_string(s)
    if not channel_re.match(s):
	raise ValueError,"Bad channel name: %r" % (s,)
    return s

def channel_to_node(ch,encoding):
    s=unescape_node_string(ch)
    n=unicode(s,encoding,"strict")
    return n

def node_to_nick(n,encoding):
    s=n.encode(encoding,"strict")
    s=escape_node_string(s)
    if not nick_re.match(s):
	raise ValueError,"Bad channel name: %r" % (s,)
    return s

def nick_to_node(ch,encoding):
    s=unescape_node_string(ch)
    n=unicode(s,encoding,"strict")
    return n

irc_translate_table=string.maketrans(
	string.ascii_uppercase+"[]\\~",
	string.ascii_lowercase+"{}|^")

def normalize(s):
    return s.translate(irc_translate_table)

class IRCUser:
    def __init__(self,session,nick,user="",host=""):
	self.session=session
	if "!" in nick:
	    nick,tmp=nick.split("!",1)
	    if "@" in tmp:
		user,host=tmp.split("@",1)
	    else:
		user=tmp
		host=""
	self.nick=nick
	self.user=user
	self.host=host
	self.mode={}
	self.channels={}

    def join_channel(self,channel):
	self.channels[normalize(channel.name)]=channel
	channel.sync_user(self)

    def leave_channel(self,channel):
	try:
	    del self.channels[normalize(channel.name)]
	    channel.sync_user(self)
	except KeyError:
	    pass

    def leave_all(self):
	for channel in self.channels.values():
	    self.leave_channel(channel)

    def sync_all(self):
	for channel in self.channels.values():
	    channel.sync_user(self)

    def whoreply(self,params):
	if params[5]!=self.nick:
	    return
	if len(params)!=8:
	    return
	target,channel,user,host,server,nick,flags,rest=params
	fullname=rest.split(None,1)[1]
	self.debug("Channel: %r" % (channel,))
	if channel and channel!="*":
	    channel=self.session.channels.get(normalize(channel))
	    if not channel:
		self.debug("Ignoring WHO reply: %r - unknown channel" % (params,))
		return
	else:
	    channel=None
	self.nick=nick
	self.host=host
	self.user=user
	if channel:
	    self.debug("Channel: %r" % (channel,))
	    self.join_channel(channel)
	    if "@" in flags:
		channel.set_mode("o",self)
	    elif "+" in flags:
		channel.set_mode("v",self)
	    else:
		channel.reset_mode("o",self)
		channel.reset_mode("v",self)
	if "G" in flags:
	    self.mode["a"]=1
	else:
	    self.mode["a"]=0
	if channel:
	    channel.sync_user(self)

    def jid(self):
	return JID(nick_to_node(self.nick,self.session.default_encoding),
		self.session.network.jid.domain,
		unicode(self.user+'@'+self.host,self.session.default_encoding,"replace"))
	
    def debug(self,msg):
	return self.session.debug(msg)

class Channel:
    toggle_modes="aimnqpsrt"
    arg_modes="kl"
    multiarg_modes="OovbeI"
    def __init__(self,session,name):
	if not channel_re.match(name):
	    raise ValueError,"Bad channel name"
	self.name=name
	self.session=session
	self.state=None
	self.stanza=None
	self.room_jid=None
	self.encoding=session.default_encoding
	self.modes={}
	self.users=[]
	self.muc=0

    def sync_user(self,user):
	if user.channels.has_key(normalize(self.name)):
	    if user not in self.users:
		self.users.append(user)
	else:
	    if user in self.users:
		self.users.remove(user)
		self.send_notice_message(u"%s has quit" 
			% (unicode(user.nick,self.encoding,"replace"),))
	if self.state:
	    p=self.get_user_presence(user)
	    self.session.component.send(p)

    def send_notice_message(self,msg):
	if not self.state or self.muc:
	    return
	m=Message(fr=self.room_jid.bare(),to=self.session.jid,type="groupchat",body=msg)
	self.session.component.send(m)

    def join(self,stanza):
	if self.state:
	    self.debug("Channel %r not in the initial state, not joining!" % (self.name,))
	    p=stanza.make_error_response(stanza,"bad-request")
	    self.session.component.send(p)
	    return
	self.room_jid=stanza.get_to()
	self.debug("Joining channel %r" % (self.name,))
	self.session.send("JOIN %s" % (self.name,))
	self.stanza=stanza.copy()
	self.state="join"
	if stanza.get_join_info():
	    self.muc=1

    def leave(self,stanza):
	status=stanza.get_status()
	if not self.state:
	    self.debug("Channel %r in the initial state - nothing to do." % (self.name,))
	else:
	    if status:
		self.session.send("PART %s" % (self.name,))
	    else:
		self.session.send("PART %s :%s" % (self.name,
			status.encode(self.encoding,"replace")))
	    self.state=None
	    self.stanza=None
	p=MucPresence(type="unavailable",fr=stanza.get_to(),to=stanza.get_from(),status=status)
	self.session.component.send(p)
	for u in self.users:
	    u.leave_room(self)
	self.state=None

    def prefix_to_jid(self,prefix):
	if "!" in prefix:
	    return self.nick_to_jid(prefix.split("!")[0])
	return self.nick_to_jid(prefix)

    def nick_to_jid(self,nick):
	return JID(self.room_jid.node,self.room_jid.domain,
		unicode(nick,self.encoding,"replace"))

    def get_user_presence(self,user):
	if self.state and user in self.users:
	    p=MucPresence(fr=self.nick_to_jid(user.nick),to=self.session.jid)
	else:
	    p=MucPresence(type="unavailable",fr=self.nick_to_jid(user.nick),to=self.session.jid)
	if self.muc:
	    ui=p.make_muc_userinfo()
	    it=MucItem("none","participant",user.jid(),unicode(user.nick,self.encoding,"replace"))
	    ui.add_item(it)
	return p

    def nick_changed(self,oldnick,user):
	p_aval=self.get_user_presence(user)
	p_unaval=p_aval.copy()
	p_unaval.set_type("unavailable")
	p_unaval.set_show(None)
	p_unaval.set_status(None)
	p_unaval.set_from(self.nick_to_jid(oldnick))
	self.session.component.send(p_unaval)
	self.session.component.send(p_aval)
	self.send_notice_message(u"%s is now known as %s" 
		% (unicode(oldnick,self.encoding,"replace"),
		    unicode(user.nick,self.encoding,"replace")))
	
    def set_mode(self,mode,arg):
	if mode in self.toggle_modes:
	    self.modes[mode]=1
	elif mode in self.arg_modes:
	    self.modes[mode]=arg
	elif mode in self.multiarg_modes:
	    if self.modes.has_key(mode):
		self.modes[mode].append(arg)
	    else:
		self.modes[mode]=[arg]

    def reset_mode(self,mode,arg):
	try:
	    if mode in self.toggle_modes:
		del self.modes[mode]
	    elif mode in self.arg_modes:
		del self.modes[mode]
	    elif mode in self.multiarg_modes:
		if self.modes.has_key(mode):
		    self.modes[mode].remove(arg)
		    if not self.modes[mode]:
			del self.modes[mode]
	except (KeyError,ValueError):
	    pass

    def irc_cmd_JOIN(self,prefix,command,params):
	nprefix=normalize(prefix)
	nnick=normalize(self.session.nick)
	if nprefix==nnick or nprefix.startswith(nnick+"!"):
	    if self.state=="join":
		self.debug("Channel %r joined!" % (self.name,))
		self.session.user.join_channel(self)
		self.state="joined"
		self.stanza=None
		self.session.send("WHO %s" % (self.name,))
	else:
	    user=self.session.get_user(prefix)
	    user.join_channel(self)
	    self.send_notice_message(u"%s has joined" 
		    % (unicode(user.nick,self.encoding,"replace"),))
	    self.session.send("WHO %s" % (user.nick,))

    def irc_cmd_PART(self,prefix,command,params):
        user=self.session.get_user(prefix)
	try:
	    self.users.remove(user)
	except ValueError:
	    pass
	user.leave_channel(self)
	self.send_notice_message(u"%s has left" 
		% (unicode(user.nick,self.encoding,"replace"),))

    def irc_cmd_PRIVMSG(self,prefix,command,params):
	self.debug("Message on channel %r" % (self.name,))
	if not self.state or len(params)<2:
	    self.debug("ignoring it")
	    return
	body=unicode(params[1],self.encoding,"replace")
	if body[0]=="\001" and body[-1]=="\001":
	    self.CTCP(prefix,body[1:-1])
	else:
	    m=Message(type="groupchat",fr=self.prefix_to_jid(prefix),to=self.session.jid,
		    body=remove_evil_characters(body))
	    self.session.component.send(m)
    
    def CTCP(self,prefix,command):
	if " " in command:
	    command,arg=command.split(" ",1)
	else:
	    arg=None
	if command=="ACTION":
	    m=Message(type="groupchat",fr=self.prefix_to_jid(prefix),to=self.session.jid,
		    body="/me "+remove_evil_characters(arg))
	    self.session.component.send(m)
	else:
	    self.debug("Unknown CTCP command: %r %r" % (command,arg))
	    
    def debug(self,msg):
	return self.session.debug(msg)


class IRCSession:
    commands_dont_show=[]
    def __init__(self,component,config,jid,nick):
	self.component=component
	self.config=config
	self.network=config.network
	self.default_encoding=self.network.default_encoding
	nick=nick.encode(self.default_encoding,"strict")
	if not nick_re.match(nick):
	    raise ValueError,"Bad nickname"
	self.jid=jid
	self.nick=nick
	self.thread=threading.Thread(name=u"%s on %s as %s" % (jid,config.network.jid,nick),
		target=self.thread_loop)
	self.exit=0
	self.socket=None
	self.lock=threading.RLock()
	self.cond=threading.Condition(self.lock)
	self.servers_left=self.network.get_servers()
	self.thread.setDaemon(1)
	self.thread.start()
	self.input_buffer=""
	self.used_for=[]
	self.server=""
	self.ready=0
	self.channels={}
	self.users={}
	self.user=IRCUser(self,nick)

    def register_user(self,user):
	self.lock.acquire()
	try:
	    self.users[normalize(user.nick)]=user
	finally:
	    self.lock.release()

    def unregister_user(self,user):
	self.lock.acquire()
	try:
	    nnick=normalize(user.nick)
	    if self.users.get(nnick)==user:
		del self.users[nnick]
	finally:
	    self.lock.release()

    def rename_user(self,user,new_nick):
	self.lock.acquire()
	try:
	    self.users[normalize(new_nick)]=user
	    try:
		del self.users[normalize(user.nick)]
	    except KeyError:
		pass
	    user.nick=new_nick
	finally:
	    self.lock.release()

    def get_user(self,prefix):
	if "!" in prefix:
	    nick=prefix.split("!",1)[0]
	else:
	    nick=prefix
	nnick=normalize(nick)
	if self.users.has_key(nnick):
	    return self.users[nnick]
	user=IRCUser(self,prefix)
	self.register_user(user)
	return user

    def check_nick(self,nick):
	nick=nick.encode(self.default_encoding)
	if normalize(nick)==normalize(self.nick):
	    return 1
	else:
	    return 0

    def thread_run(self):
	try:
	    self.thread_loop()
	except:
	    self.print_exception()
	self.lock.acquire()
	try:
	    if self.socket:
		try:
		    self.socket.close()
		except:
		    pass
		self.socket=None
	    try:
		del self.component.irc_sessions[self.jid.as_unicode()]
	    except KeyError:
		pass
	finally:
	    self.lock.release()
    
    def thread_loop(self):
	self.debug("thread_loop()")
	while not self.exit:
	    self.lock.acquire()
	    try:
		if self.socket is None:
		    self._try_connect()
		sock=self.socket
		self.lock.release()
		if sock is None:
		    continue
		id,od,ed=select.select([sock],[],[sock],1)
		self.lock.acquire()
		if self.socket in id:
		    self.input_buffer+=self.socket.recv(1024)
		    while self.input_buffer.find("\r\n")>-1:
			input,self.input_buffer=self.input_buffer.split("\r\n",1)
			self._process_input(input)
	    finally:
		self.lock.release()
	self.lock.acquire()
	try:
	    if self.socket:
		self.socket.close()
		self.socket=None
	finally:
	    self.lock.release()

    def _try_connect(self):
	if not self.servers_left:
	    self.debug("No servers left, quitting")
	    self.exit=1
	    return
	if self.socket:
	    self.socket.close()
	    self.socket=None
	server=self.servers_left.pop(0)
	self.debug("Trying to connect to %r" % (server,))
	try:
	    self.socket=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
	    self.socket.connect(server)
	except (IOError,OSError,socket.error),err:
	    self.debug("Server connect error: %r" % (err,))
	    if self.socket:
		try:
		    self.socket.close()
		except:
		    pass
	    self.socket=None
	    return
	self._send("NICK %s" % (self.nick,))
	user=sha.new(self.jid.bare().as_string()).hexdigest()
	self._send("USER %s 0 * :JJIGW User %s" % (user,user))
	self.server=server[0]
	self.ready=1
	self.cond.notify()

    def _send(self,str):
	self.debug("IRC OUT: %r" % (str,))
	self.socket.send(str+"\r\n")

    def send(self,str):
	self.lock.acquire()
	try:
	    self._send(str)
	finally:
	    self.lock.release()

    def _process_input(self,input):
	self.debug("Server message: %r" % (input,))
	split=input.split(" ")
	if split[0].startswith(":"):
	    prefix=split[0][1:]
	    split=split[1:]
	else:
	    prefix=None
	if split:
	    command=split[0]
	    split=split[1:]
	else:
	    command=None
	params=[]
	while split:
	    if split[0].startswith(":"):
		params.append(string.join(split," ")[1:])
		break
	    params.append(split[0])
	    split=split[1:]
	self.debug("Prefix: %r Command: %r params: %r" % (prefix,command,params))
	self.lock.release()
	try:
	    f=None
	    for c in self.channels.keys():
		if params and normalize(params[0])==c:
		    f=getattr(self.channels[c],"irc_cmd_"+command,None)
		    if f:
			break
	    if not f:
		f=getattr(self,"irc_cmd_"+command,None)
	    if f:
		f(prefix,command,params)
	    else:
		for u in self.used_for:
		    self.debug("u: %r" % (u,))
		    if u.bare()==self.network.jid:
			self.pass_input_to_user(prefix,command,params)
			break
	finally:
	    self.lock.acquire()

    def irc_cmd_PING(self,prefix,command,params):
	self.send("PONG %s" % (params[0],))

    def irc_cmd_NICK(self,prefix,command,params):
	if len(params)<1:
	    return
	user=self.get_user(prefix)
	if params[0]!=user.nick:
	    oldnick=user.nick
	    self.rename_user(user,params[0])
	    for ch in user.channels.values():
		ch.nick_changed(oldnick,user)

    def irc_cmd_QUIT(self,prefix,command,params):
	user=self.get_user(prefix)
	user.leave_all()
	self.unregister_user(user)

    def irc_cmd_352(self,prefix,command,params):
	self.debug("WHO reply received")
	if len(params)<8:
	    self.debug("too short - ignoring")
	    return
	user=self.get_user(params[5])
	self.debug("Got user %r" % (user.nick,))
	user.whoreply(params)
	self.debug("%r on channels %r" % (user.nick,user.channels.keys()))
	for c in user.channels.keys():
	    self.debug("announcing %r presence on channel %r" % (user.nick,c))
	    channel=user.channels[c]
	    self.component.send(channel.get_user_presence(user))
	    
    def pass_input_to_user(self,prefix,command,params):
	if command in self.commands_dont_show:
	    return
	nprefix=normalize(prefix)
	nnick=normalize(self.nick)
	nserver=normalize(self.server)
	if nprefix==nnick or prefix and nprefix.startswith(nnick+"!"):
	    return
	if nprefix==nserver and len(params)==2 and params[0]==self.nick:
	    body=u"(!) %s" % (unicode(params[1],self.default_encoding,"replace"),)
	elif command in ("004","005","252","253","254"):
	    p=string.join(params[1:]," ")
	    body=u"(!) %s" % (unicode(p,self.default_encoding,"replace"),)
	elif prefix:
	    body=u"(%s) %s %r" % (prefix,command,params)
	else:
	    body=u"%s %r" % (command,params)
	fr=JID(None,self.network.jid.domain,self.server)
	m=Message(to=self.jid,fr=fr,body=body)
	self.component.send(m)

    def join(self,stanza):
	to=stanza.get_to()
	channel=node_to_channel(to.node,self.default_encoding)
	if self.channels.has_key(normalize(channel)):
	    return
	self.cond.acquire()
	try:
	    # FIXME: may hang the main thread
	    while not self.ready and not self.exit:
		self.cond.wait()
	finally:
	    self.cond.release()
	if self.exit:
	    return
	channel=Channel(self,channel)
	channel.join(stanza)
	self.channels[normalize(channel.name)]=channel

    def message_to_channel(self,stanza):
	if not self.ready:
	    return
	channel=stanza.get_to().node
	channel=node_to_channel(channel,self.default_encoding)
	if not channel_re.match(channel):
	    debug("Bad channel name: %r" % (channel,))
	    return
	body=stanza.get_body().encode(self.default_encoding,"replace")
	body=body.replace("\n"," ").replace("\r"," ")
	if body.startswith("/me "):
	    body="\001ACTION "+body[4:]+"\001"
	self.send("PRIVMSG %s :%s" % (channel,body))
	channel=self.channels.get(normalize(channel))
	if channel:
	    channel.irc_cmd_PRIVMSG(self.nick,"PRIVMSG",[channel.name,body])

    def disconnect(self,reason):
	if not reason:
	    reason="Unknown reason"
	self.send("QUIT :%s" % (reason,))
	self.exit=1

    def debug(self,msg):
	self.component.debug(msg)
    
    def print_exception(self):
	self.component.print_exception()

class Component(pyxmpp.jabberd.Component):
    def __init__(self,config):
	pyxmpp.jabberd.Component.__init__(self,config.network.jid,
		config.connect.secret,config.connect.host,config.connect.port,
		category="gateway",type="irc")
	self.exit=0
	self.irc_sessions={}
	self.config=config

    def send(self,stanza):
	self.get_stream().send(stanza)

    def stream_state_changed(self,state,arg):
	print "*** State changed: %s %r ***" % (state,arg)

    def authenticated(self):
	pyxmpp.jabberd.Component.authenticated(self)
	self.stream.set_iq_get_handler("query","jabber:iq:version",self.get_version)
	self.stream.set_iq_get_handler("query","jabber:iq:register",self.get_register)
	self.stream.set_iq_set_handler("query","jabber:iq:register",self.set_register)
	self.disco_info.add_feature("jabber:iq:version")
	self.disco_info.add_feature("jabber:iq:register")
	self.disco_info.add_feature(MUC_NS)
	self.stream.set_presence_handler("available",self.presence_available)
	self.stream.set_presence_handler("unavailable",self.presence_unavailable)
	self.stream.set_presence_handler("subscribe",self.presence_control)
	self.stream.set_message_handler("groupchat",self.groupchat_message)

    def get_version(self,iq):
	iq=iq.make_result_response()
	q=iq.new_query("jabber:iq:version")
	q.newTextChild(q.ns(),"name","Jajcus' Jabber-IRC Gateway")
	q.newTextChild(q.ns(),"version","0.1")
	self.stream.send(iq)
	return 1
    
    def get_register(self,iq):
	to=iq.get_to()
	if to and to!=self.jid:
	    iq=iq.make_error_response("feature-not-implemented")
	    self.stream.send(iq)
	    return 1
	iq=iq.make_result_response()
	q=iq.new_query("jabber:iq:register")
	q.newTextChild(q.ns(),"instructions","Enter anything below.")
	q.newChild(q.ns(),"username",None)
	q.newChild(q.ns(),"password",None)
	self.stream.send(iq)
	return 1

    def set_register(self,iq):
	to=iq.get_to()
	if to and to!=self.jid:
	    iq=iq.make_error_response("feature-not-implemented")
	    self.stream.send(iq)
	    return 1
	remove=iq.xpath_eval("r:query/r:remove",{"r":"jabber:iq:register"})
	if remove:
	    m=Message(fr=iq.get_to(),to=iq.get_from(),type="chat",
		    body=u"Unregistered")
	    self.stream.send(m)
	    p=Presence(fr=iq.get_to(),to=iq.get_from(),type="unsubscribe")
	    self.stream.send(p)
	    p=Presence(fr=iq.get_to(),to=iq.get_from(),type="unsubscribed")
	    self.stream.send(p)
	    return 1
	username=iq.xpath_eval("r:query/r:username",{"r":"jabber:iq:register"})
	if username:
	    username=username[0].getContent()
	else:
	    username=u""
	password=iq.xpath_eval("r:query/r:password",{"r":"jabber:iq:register"})
	if password:
	    password=password[0].getContent()
	else:
	    password=u""
	m=Message(fr=iq.get_to(),to=iq.get_from(),type="chat",
		body=u"Registered with username '%s' and password '%s'"
		" (both ignored)" % (username,password))
	self.stream.send(m)
	p=Presence(fr=iq.get_to(),to=iq.get_from(),type="subscribe")
	self.stream.send(p)
	iq=iq.make_result_response()
	self.stream.send(iq)
	return 1

    def groupchat_message(self,stanza):
	to=stanza.get_to()
	if to.resource:
	    self.debug("Groupchat message target is not bare JID")
	    return
	if not to.node:
	    self.debug("No node in groupchat message target")
	    return
	fr=stanza.get_from()	
	sess=self.irc_sessions.get(fr.as_unicode())
	if sess:
	    sess.message_to_channel(stanza)
	else:
	    m=stanza.make_error_response("recipient-unavailable")
	    self.send(m)
	return 1

    def presence_available(self,stanza):
	nick=None
	to=stanza.get_to()
	fr=stanza.get_from()
	status=stanza.get_status()
	if not status:
	    status="Unknown"
	if to.node and not to.resource:
	    p=stanza.make_error_response("bad-request")
	    self.send(p)
	    return 1
	sess=self.irc_sessions.get(fr.as_unicode())
	if sess:
	    if to.node and not sess.check_nick(to.resource):
		p=stanza.make_error_response("conflict")
		self.send(p)
		return 1
	    if to not in sess.used_for:
		sess.used_for.append(to)
	else:
	    nick=to.resource
	    if not nick:
		nick=fr.node
	    sess=IRCSession(self,self.config,fr,nick)
	    sess.used_for.append(to)
	    self.irc_sessions[fr.as_unicode()]=sess
	if to.node:
	    sess.join(MucPresence(stanza))
	else:
	    p=Presence(
		to=stanza.get_from(),
		fr=stanza.get_to(),
		show=stanza.get_show(),
		status=stanza.get_status()
		);
	    self.send(p)
	return 1

    def presence_unavailable(self,stanza):
	to=stanza.get_to()
	fr=stanza.get_from()
	status=stanza.get_status()
	sess=self.irc_sessions.get(fr.as_unicode())
	if sess:
	    try:
		sess.used_for.remove(to)
	    except ValueError:
		pass
	    if not sess.used_for:
		sess.disconnect(status)
		try:
		    del self.irc_sessions[fr.as_unicode()]
		except KeyError:
		    pass
	p=Presence(
	    type="unavailable",
	    to=stanza.get_from(),
	    fr=stanza.get_to()
	    );
	self.stream.send(p)
	return 1


    def presence_control(self,stanza):
	p=stanza.make_accept_response()
	self.stream.send(p)
	return 1


config=Config("jjigw.xml")

print "creating component..."
c=Component(config)

print "connecting..."
c.connect()

print "looping..."
try:
    c.loop(1)
except KeyboardInterrupt:
    print "disconnecting..."
    c.disconnect()
    pass

print "exiting..."

# vi: sw=4 ts=8 sts=4
