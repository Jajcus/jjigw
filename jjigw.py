#!/usr/bin/python -u

import sys
import libxml2
import threading
import re
import select
import socket
import md5
import string
import random
import signal
import Queue
import time
from types import StringType,UnicodeType

from pyxmpp import ClientStream,JID,Iq,Presence,Message,StreamError
import pyxmpp.jabberd
from pyxmpp.jabber.muc import MucPresence,MucX,MucUserX,MucItem,MUC_NS,MucStatus
from pyxmpp.jabber.muc import MucIq,MucAdminQuery,MUC_ADMIN_NS

class JJIGWFatalError(RuntimeError):
    pass

evil_characters_re=re.compile(r"[\000-\010\013\014\016-\037]")
def remove_evil_characters(s):
    return evil_characters_re.sub(" ",s)

color_re=re.compile(r"\x03\d\d|\x0f")
def strip_colors(s):
    return color_re.sub("",s)

numeric_re=re.compile(r"\d\d\d")
channel_re=re.compile(r"^[&#+!][^\000 \007 ,:\r\n]{1,49}$")
nick_re=re.compile(r"^[a-zA-Z\x5b-\x60\x7b-\x7d\[\]\\`_^{|}][a-zA-Z\x5b-\x60\x7b-\x7d\[\]\\`_^{|}0-9-]{0,8}$")
nick8_re=re.compile(r"^[a-zA-Z\x5b-\x60\x7b-\x7d\[\]\\`_^{|}\x80-\xff][a-zA-Z\x5b-\x60\x7b-\x7d\[\]\\`_^{|}0-9\x80-\xff-]{0,8}$")

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

def node_to_nick(n,encoding,network):
    s=n.encode(encoding,"strict")
    s=escape_node_string(s)
    if not network.valid_nick(s):
	raise ValueError,"Bad nick name: %r" % (s,)
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

class ConnectConfig:
    def __init__(self,node):
	self.node=node
	self.host=node.xpathEval("host")[0].getContent()
	self.port=int(node.xpathEval("port")[0].getContent())
	self.secret=node.xpathEval("secret")[0].getContent()

class SPIdentDConfig:
    def __init__(self,node):
	node=node.xpathEval("socket")[0]
	self.socket=node.getContent()

class ServerConfig:
    def __init__(self,node):
	self.host=node.getContent()
	self.port=node.prop("port")
	try:
	    self.port=int(self.port)
	    if self.port<1 or self.port>65535:
		raise ValueError
	except ValueError:
	    print >>sys.stderr,"Bad port value: %r, using default: 6667" % (self.port,)
	    self.port=6667

class ChannelConfig:
    def __init__(self,node):
	self.name=node.getContent()
	self.encoding=node.prop("encoding")

class NetworkConfig:
    def __init__(self,node):
	self.node=node
	self.jid=JID(node.prop("jid"))
	servers=node.xpathEval("server")
	self.servers=[]
	for s in servers:
	    self.servers.append(ServerConfig(s))
	channels=node.xpathEval("channel")
	self.channels={}
	if channels:
	    for c in channels:
		ch=ChannelConfig(c)
		self.channels[normalize(ch.name)]=ch
	self.default_encoding=node.prop("encoding")
	self.nicks_8bit=node.prop("nicks_8bit")
    def get_servers(self):
	r=self.servers
	self.servers=self.servers[-1:]+self.servers[1:]
	return r
    def get_channel_config(self,channel):
	return self.channels.get(normalize(channel))
    def valid_nick(self,s):
	if self.nicks_8bit:
	    m=nick8_re.match(s)
	else:
	    m=nick_re.match(s)
	if m:
	    return 1
	else:
	    return 0

class Config:
    def __init__(self,filename):
	self.doc=None
	parser=libxml2.createFileParserCtxt(filename)
	parser.validate(1)
	parser.parseDocument()
	if not parser.isValid():
	    raise JJIGWFatalError,"Invalid configuration"
	self.doc=parser.doc()
	self.connect=ConnectConfig(self.doc.xpathEval("jjigw/connect")[0])
	self.network=NetworkConfig(self.doc.xpathEval("jjigw/network")[0])
	spidentd=self.doc.xpathEval("jjigw/spidentd")
	if spidentd:
	    self.spidentd=SPIdentDConfig(spidentd[0])
	else:
	    self.spidentd=None
    def __del__(self):
	if self.doc:
	    self.doc.freeDoc()


class Request:
    def __init__(self,command,stanza,args=None):
	self.command=command
	self.stanza=stanza
	self.args=args
    def match(self,commands,args=None):
	if type(commands) in (StringType,UnicodeType):
	    commands=[commands]
	for c in commands:
	    if not self.command==c:
		continue
	    if args and not self.args==args:
		continue
	    return 1
	return 0

class RequestQueue:
    def __init__(self,maxsize):
	self.maxsize=maxsize
	self.requests=[]
    def get(self,commands,args=None):
	for r in self.requests:
	    if r.match(commands):
		try:
		    self.requests.remove(r)
		except ValueError:
		    pass
		return r
	return None
    def add(self,command,stanza,args=None):
	r=Request(command,stanza,args)
	self.requests.append(r)
	if len(self.requests)>10:
	    self.requests=self.requests[-10:]

class IRCUser:
    def __init__(self,session,nick,user="",host=""):
	self.sync_delay=0
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
	self.current_thread=None

    def descr(self):
	if self.user and self.host:
	    return "%s(%s@%s)" % (self.nick,self.user,self.host)
	else:
	    return self.nick

    def sync_in_channel(self,channel,status=None):
	if self.sync_delay>0:
	    return
	elif self.sync_delay<0:
	    self.debug("Warning: %r.sync_delay<0" % (self,))
	return channel.sync_user(self,status=status)

    def join_channel(self,channel):
	self.channels[normalize(channel.name)]=channel
	self.sync_in_channel(channel)

    def leave_channel(self,channel,status=None):
	try:
	    del self.channels[normalize(channel.name)]
	    self.sync_in_channel(channel,status=status)
	except KeyError:
	    pass

    def leave_all(self):
	for channel in self.channels.values():
	    self.leave_channel(channel)

    def sync_all(self):
	for channel in self.channels.values():
	    self.sync_in_channel(channel)

    def whoreply(self,params):
	if params[4]!=self.nick:
	    return
	if len(params)!=7:
	    return
	channel,user,host,server,nick,flags,rest=params
	fullname=rest.split(None,1)[1]
	if channel and channel!="*":
	    channel=self.session.channels.get(normalize(channel))
	    if not channel:
		self.debug("Ignoring WHO reply: %r - unknown channel" % (params,))
		return
	else:
	    channel=None
	self.sync_delay+=1
	try:
	    self.nick=nick
	    self.host=host
	    self.user=user
	    if channel:
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
	finally:
	    self.sync_delay-=1
	if channel:
	    channel.sync_user(self)

    def jid(self):
	return JID(nick_to_node(self.nick,self.session.default_encoding),
		self.session.network.jid.domain,
		unicode(self.user+'@'+self.host,self.session.default_encoding,"replace"))

    def __repr__(self):
	return "<IRCUser %r>" % (self.nick,)

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
	self.room_jid=None
	self.config=session.network.get_channel_config(name)
	if self.config and self.config.encoding:
	    self.encoding=self.config.encoding
	else:
	    self.encoding=session.default_encoding
	self.modes={}
	self.users=[]
	self.muc=0
	self.requests=RequestQueue(10)

    def sync_user(self,user,status=None):
	if user.channels.has_key(normalize(self.name)):
	    if user not in self.users:
		self.users.append(user)
	else:
	    for m in self.multiarg_modes:
		ul=self.modes.get(m,[])
		if user in ul:
		    ul.remove(user)
	    if user in self.users:
		self.users.remove(user)
		self.send_notice_message(u"%s has quit" 
			% (unicode(user.nick,self.encoding,"replace"),))
	if self.state:
	    p=self.get_user_presence(user,status=status)
	    self.session.component.send(p)

    def send_notice_message(self,msg,not_in_muc=1):
	if not self.state or (self.muc and not_in_muc):
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
	self.requests.add("JOIN",stanza)
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

    def get_user_presence(self,user,nick=None,actor=None,reason=None,status=None):
	if self.state and user in self.users:
	    p=MucPresence(fr=self.nick_to_jid(user.nick),to=self.session.jid)
	else:
	    p=MucPresence(type="unavailable",fr=self.nick_to_jid(user.nick),to=self.session.jid)
	if self.muc:
	    if user in self.modes.get("o",[]):
		aff="admin"
		role="moderator"
	    elif user in self.modes.get("v",[]):
		aff="member"
		role="participant"
	    elif self.modes.get("m"):
		aff="none"
		role="visitor"
	    elif user in self.users:
		aff="none"
		role="participant"
	    else:
		aff="none"
		role="none"
	    ui=p.make_muc_userinfo()
	    if nick:
		nick=unicode(user.nick,self.encoding,"replace")
	    it=MucItem(aff,role,user.jid(),nick=nick,actor=actor,reason=reason)
	    ui.add_item(it)
	    if status:
		ui.add_item(MucStatus(status))
	return p

    def nick_changed(self,oldnick,user):
	p_unaval=self.get_user_presence(user,nick=user.nick,status=303)
	p_unaval.set_type("unavailable")
	p_unaval.set_show(None)
	p_unaval.set_status(None)
	p_unaval.set_from(self.nick_to_jid(oldnick))
	p_aval=self.get_user_presence(user,status=303)
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

    def irc_cmd_324(self,prefix,command,params): # RPL_CHANNELMODEIS
	for m in self.toggle_modes:
	    try:
		del self.modes[m]
	    except KeyError:
		pass
	self.irc_mode_changed(prefix,command,params)
	
    def irc_cmd_482(self,prefix,command,params): # ERR_CHANOPRIVSNEEDED
	self.irc_error_response(prefix,command,params,["TOPIC","KICK","MODE"],"forbidden")

    def irc_cmd_461(self,prefix,command,params): # ERR_NEEDMOREPARAMS
	self.irc_error_response(prefix,command,params,["TOPIC","KICK","MODE"],"bad-request")

    def irc_cmd_403(self,prefix,command,params): # ERR_NOSUCHCHANNEL
	self.irc_error_response(prefix,command,params,["KICK"],"recipient-unavailable")
    
    def irc_cmd_476(self,prefix,command,params): # ERR_BADCHANMASK
	self.irc_error_response(prefix,command,params,["KICK"],"bad-request")
    
    def irc_cmd_441(self,prefix,command,params): # ERR_USERNOTINCHANNEL
	self.irc_error_response(prefix,command,params,["KICK","MODE"],"item-not-found")
	
    def irc_cmd_442(self,prefix,command,params): # ERR_NOTONCHANNEL
	self.irc_error_response(prefix,command,params,["TOPIC","KICK"],"forbidden")

    def irc_cmd_472(self,prefix,command,params): # ERR_UNKNOWNMODE
	self.irc_error_response(prefix,command,params,["MODE"],"feature-not-implemented")

    def irc_cmd_477(self,prefix,command,params): # ERR_NOCHANMODES
	self.irc_error_response(prefix,command,params,["TOPIC","MODE"],"not-acceptable")

    def irc_error_response(self,prefix,command,params,requests,condition):
	command,stanza=self.requests.get(requests)
	if command:
	    m=stanza.make_error_response(condition)
	else:
	    m=Message(fr=self.room_jid.bare(),to=self.session.jid,
		    type="error", error_cond=condition)
	self.session.component.send(m)

    def irc_cmd_331(self,prefix,command,params): # RPL_NOTOPIC
	m=Message(fr=self.room_jid.bare(),to=self.session.jid, type="groupchat", subject=u"")
	self.session.component.send(m)
	
    def irc_cmd_332(self,prefix,command,params): # RPL_TOPIC
	topic=remove_evil_characters(params[1])
	m=Message(fr=self.room_jid.bare(),to=self.session.jid,
		type="groupchat", subject=unicode(topic,self.encoding,"replace"))
	self.session.component.send(m)

    def irc_cmd_TOPIC(self,prefix,command,params):
	self.requests.get("TOPIC")
	topic=remove_evil_characters(params[1])
	m=Message(fr=self.prefix_to_jid(prefix),to=self.session.jid,
		type="groupchat", subject=unicode(topic,self.encoding,"replace"))
	self.session.component.send(m)
	
    def irc_cmd_MODE(self,prefix,command,params):
	if len(params)<2:
	    self.debug("No parameters in received MODE")
	    return
	params_str=string.join(params[2:]," ").strip()
	if params_str:
	    params_str=" "+params_str
	if "!" in prefix:
	    nick,iuser=prefix.split("!",1)
	    iuser="(%s)" % (iuser,)
	else:
	    nick,iuser=prefix,""
	self.send_notice_message(u"Mode change: [%s%s] by %s%s" 
		% (unicode(params[1],self.encoding,"replace"),
			unicode(params_str,self.encoding,"replace"),
			unicode(nick,self.encoding,"replace"),
			unicode(iuser,self.encoding,"replace")),
		0)
	if self.session.check_prefix(prefix) and len(params)>=3:
	    r=self.requests.get("MODE",(params[1],normalize(params[2])))
	    if r:
		reply=r.stanza.make_result_response()
		self.session.component.send(reply)
	self.irc_mode_changed(prefix,command,params)

    def irc_mode_changed(self,prefix,command,params):
	actor=self.session.get_user(prefix)
	modes=params[1]
	params=params[2:]
	pm=None
	for m in modes:
	    if m in "+-":
		pm=m
		continue
	    elif not pm:
		self.debug("Not '+' or '-' before '%s' in received MODE" % (m,))
		continue
	    elif m in self.arg_modes or m in self.multiarg_modes:
		if not len(params):
		    self.debug("No argument for mode '%s' in received MODE" % (m,))
		    continue
		arg=params.pop(0)
	    elif m in self.toggle_modes:
		arg=None
	    else:
		self.debug("Unknown mode '%s' in received MODE" % (m,))
		continue
	    if m in "oOv":
		arg=self.session.get_user(arg)
		if not arg:
		    continue
	    if pm=="+":
		self.set_mode(m,arg)
	    else:
		self.reset_mode(m,arg)
	    if m in "oOv":
		self.user_mode_changed(arg,actor,m)
	    elif m=="m":
		for u in self.users:
		    self.sync_user(u)

    def user_mode_changed(self,user,actor,mode):
	if actor:
	    actor_jid=self.nick_to_jid(actor.nick)
	else:
	    actor_jid=None
	p=self.get_user_presence(user,actor_jid)
	if actor:
	    by=u" by %s" % (unicode(actor.nick,self.encoding,"replace"),)
	else:
	    by=u""
	self.session.component.send(p)
	if mode=="v":
	    self.send_notice_message(u"%s was granted voice%s" 
		    % (unicode(user.nick,self.encoding,"replace"),by))
	elif mode=="o":
	    self.send_notice_message(u"%s was granted operator status%s" 
		    % (unicode(user.nick,self.encoding,"replace"),by))
	elif mode=="O":
	    self.send_notice_message(u"%s was granted got owner status%s" 
		    % (unicode(user.nick,self.encoding,"replace"),by))

    def irc_cmd_JOIN(self,prefix,command,params):
	nprefix=normalize(prefix)
	nnick=normalize(self.session.nick)
	if nprefix==nnick or nprefix.startswith(nnick+"!"):
	    if self.state=="join":
		self.debug("Channel %r joined!" % (self.name,))
		self.session.user.sync_delay+=1
		try:
		    self.session.user.join_channel(self)
		finally:
		    self.session.user.sync_delay-=1
		self.state="joined"
		self.requests.get("JOIN")
		self.session.send("MODE %s" % (self.name,))
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

    def irc_cmd_KICK(self,prefix,command,params):
        actor=self.session.get_user(prefix)
        user=self.session.get_user(params[1])
	try:
	    self.users.remove(user)
	except ValueError:
	    pass
	self.send_notice_message(u"%s was kicked by %s" 
		% (unicode(user.descr(),self.encoding,"replace"),
		    unicode(actor.descr(),self.encoding,"replace")),
		0)
	user.leave_channel(self,status=307)
	if user and self.session.check_prefix(prefix):
	    r=self.requests.get("KICK",user.nick)
	    if r:
		iq=r.stanza.make_result_response()
		self.session.component.send(iq)

    def irc_cmd_PRIVMSG(self,prefix,command,params):
	self.irc_message(prefix,command,params)

    def irc_cmd_NOTICE(self,prefix,command,params):
	self.irc_message(prefix,command,params)

    def irc_message(self,prefix,command,params):
	if not self.state or len(params)<2:
	    self.debug("ignoring it")
	    return
	body=unicode(params[1],self.encoding,"replace")
	if body[0]=="\001" and body[-1]=="\001":
	    self.CTCP(prefix,body[1:-1])
	else:
	    m=Message(type="groupchat",fr=self.prefix_to_jid(prefix),to=self.session.jid,
		    body=remove_evil_characters(strip_colors(body)))
	    self.session.component.send(m)
    
    def CTCP(self,prefix,command):
	if " " in command:
	    command,arg=command.split(" ",1)
	else:
	    arg=None
	if command=="ACTION":
	    m=Message(type="groupchat",fr=self.prefix_to_jid(prefix),to=self.session.jid,
		    body="/me "+remove_evil_characters(strip_colors(arg)))
	    self.session.component.send(m)
	else:
	    self.debug("Unknown CTCP command: %r %r" % (command,arg))

    def change_topic(self,topic,stanza):
	topic=topic.encode(self.encoding,"replace")
	topic=topic.replace("\n"," ").replace("\r"," ")
	self.session.send("TOPIC %s :%s" % (self.name,topic))
	self.requests.add("TOPIC",stanza)

    def kick_user(self,nick,reason,stanza):
	nick=nick.encode(self.encoding,"strict")
	self.session.send("KICK %s %s :%s" % (self.name,nick,reason))
	self.requests.add("KICK",stanza,nick)
 
    def op_user(self,nick,stanza):
	nick=nick.encode(self.encoding,"strict")
	user=self.session.users.get(normalize(nick))
	if not user in self.users:
	   r=stanza.make_error_response("item-not-found")
	   self.session.component.send(r)
	   return
	if user in self.modes.get("o",[]):
	   r=stanza.make_result_response()
	   self.session.component.send(r)
	   return
	self.session.send("MODE %s +o %s" % (self.name,nick))
	self.requests.add("MODE",stanza,("+o",normalize(nick)))

    def __repr__(self):
	return "<IRCChannel %r>" % (self.name,)

    def debug(self,msg):
	return self.session.debug(msg)


class IRCSession:
    commands_dont_show=[]
    def __init__(self,component,config,jid,nick):
	self.component=component
	self.config=config
	self.network=config.network
	self.default_encoding=self.network.default_encoding
	self.conninfo=None
	nick=nick.encode(self.default_encoding,"strict")
	if not self.network.valid_nick(nick):
	    raise ValueError,"Bad nickname"
	self.jid=jid
	self.nick=nick
	self.thread=threading.Thread(name=u"%s on %s as %s" % (jid,config.network.jid,nick),
		target=self.thread_run)
	self.thread.setDaemon(1)
	self.exit=None
	self.exited=0
	self.socket=None
	self.lock=threading.RLock()
	self.cond=threading.Condition(self.lock)
	self.servers_left=self.network.get_servers()
	self.input_buffer=""
	self.used_for=[]
	self.server=None
	self.join_requests=[]
	self.messages_to_channel=[]
	self.messages_to_user=[]
	self.ready=0
	self.channels={}
	self.users={}
	self.user=IRCUser(self,nick)
	self.thread.start()

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

    def get_user(self,prefix,create=1):
	if "!" in prefix:
	    nick=prefix.split("!",1)[0]
	else:
	    nick=prefix
	if not self.network.valid_nick(nick):
	    return None
	nnick=normalize(nick)
	if self.users.has_key(nnick):
	    return self.users[nnick]
	if not create:
	    return None
	user=IRCUser(self,prefix)
	self.register_user(user)
	return user

    def check_nick(self,nick):
	nick=nick.encode(self.default_encoding)
	if normalize(nick)==normalize(self.nick):
	    return 1
	else:
	    return 0

    def check_prefix(self,prefix):
	if "!" in prefix:
	    nick=prefix.split("!",1)[0]
	else:
	    nick=prefix
	return normalize(nick)==normalize(self.nick)

    def prefix_to_jid(self,prefix):
	if channel_re.match(prefix):
	    node=channel_to_node(prefix,self.default_encoding)
	    return JID(node,self.network.jid.domain,None)
	else:
	    if "!" in prefix:
		nick,user=prefix.split("!",1)
	    else:
		nick=prefix 
		user=""
	    node=nick_to_node(nick,self.default_encoding)
	    resource=unicode(user,self.default_encoding,"replace")
	    return JID(node,self.network.jid.domain,resource)

    def thread_run(self):
	clean_exit=1
	try:
	    self.thread_loop()
	except:
	    clean_exit=0
	    self.print_exception()
	self.lock.acquire()
	try:
	    if not self.exited and self.socket:
		if clean_exit and self.component.shutdown:
		    self._send("QUIT :JJIGW shutdown")
		elif clean_exit and self.exit:
		    self._send("QUIT :%s" % (self.exit.encode(self.default_encoding,"replace")))
		else:
		    self._send("QUIT :Internal JJIGW error")
		self.exited=1
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
	for j in self.used_for:
	    p=Presence(fr=j,to=self.jid,type="unavailable")
	    self.component.send(p)
	self.used_for=[]
    
    def thread_loop(self):
	self.debug("thread_loop()")
	while not self.exit and not self.component.shutdown:
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
			self._safe_process_input(input)
	    finally:
		self.lock.release()
	self.lock.acquire()
	try:
	    if self.socket:
		self.socket.close()
		self.socket=None
	    if self.conninfo:
		self.component.unregister_connection(self.conninfo)
		self.conninfo=None
	finally:
	    self.lock.release()

    def _try_connect(self):
	if not self.servers_left:
	    self.debug("No servers left, quitting")
	    self.exit="No servers left, quitting"
	    return
	if self.conninfo:
	    self.component.unregister_connection(self.conninfo)
	    self.conninfo=None
	if self.socket:
	    self.socket.close()
	    self.socket=None
	server=self.servers_left.pop(0)
	self.debug("Trying to connect to %r" % (server,))
	try:
	    self.socket=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
	    self.socket.connect((server.host,server.port))
	except (IOError,OSError,socket.error),err:
	    self.debug("Server connect error: %r" % (err,))
	    if self.socket:
		try:
		    self.socket.close()
		    if self.conninfo:
			self.component.unregister_connection(self.conninfo)
			self.conninfo=None
		except:
		    pass
	    self.socket=None
	    return
	self._send("NICK %s" % (self.nick,))
	user=md5.new(self.jid.bare().as_string()).hexdigest()[:64]
	self.conninfo=ConnectionInfo(self.socket,user)
	self.component.register_connection(self.conninfo)
	self._send("USER %s 0 * :JJIGW User %s" % (user,user))
	self.server=server
	self.cond.notify()

    def _send(self,str):
	if self.socket and not self.exited:
	    self.debug("IRC OUT: %r" % (str,))
	    self.socket.send(str+"\r\n")
	else:
	    self.debug("ignoring out: %r" % (str,))

    def send(self,str):
	self.lock.acquire()
	try:
	    self._send(str)
	finally:
	    self.lock.release()

    def _safe_process_input(self,input):
	try:
	    self._process_input(input)
	except:
	    self.print_exception()
    
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
	if command and numeric_re.match(command):
	    params=params[1:]
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

    def irc_cmd_PRIVMSG(self,prefix,command,params):
	self.irc_message(prefix,command,params)

    def irc_cmd_NOTICE(self,prefix,command,params):
	self.irc_message(prefix,command,params)

    def irc_message(self,prefix,command,params):
	if len(params)<2:
	    self.debug("ignoring it")
	    return
	user=self.get_user(prefix)
	if user.current_thread:
	    typ,thread,fr=user.current_thread
	else:
	    typ="chat"
	    thread=str(random.random())
	    fr=None
	    user.current_thread=typ,thread,None
	if not fr:
	    fr=user.jid()
	body=unicode(params[1],self.default_encoding,"replace")
	m=Message(type=typ,fr=fr,to=self.jid,body=remove_evil_characters(strip_colors(body)))
	self.component.send(m)

    def login_error(self,join_condition,message_condition):
	self.lock.acquire()
	try:
	    if join_condition:
		for s in self.join_requests:
		    p=s.make_error_response(join_condition)
		    self.component.send(p)
		    try:
			self.used_for.remove(s.get_to())
		    except ValueError:
			pass
		self.join_requests=[]
	    if message_condition:
		for s in self.messages_to_user+self.messages_to_channel:
		    p=s.make_error_response(message_condition)
		    self.component.send(p)
		self.messages_to_user=[]
		self.messages_to_channel=[]
	    self.exit="IRC user registration failed"
	finally:
	    self.lock.release()

    def irc_cmd_001(self,prefix,command,params): # RPL_WELCOME
	self.lock.acquire()
	try:
	    self.debug("Connected successfully")
	    self.ready=1
	    for s in self.join_requests:
		self.join(s)
	    for s in self.messages_to_user:
		self.message_to_user(s)
	    for s in self.messages_to_channel:
		self.message_to_channel(s)
	finally:
	    self.lock.release()

    def irc_cmd_431(self,prefix,command,params): # ERR_NONICKNAMEGIVEN
	if self.ready:
	    return
	self.login_error("undefined-condition","not-authorized")
 
    def irc_cmd_432(self,prefix,command,params): # ERR_ERRONEUSNICKNAME
	if self.ready:
	    return
	self.login_error("bad-request","not-authorized")
 
    def irc_cmd_433(self,prefix,command,params): # ERR_NICKNAMEINUSE
	if self.ready:
	    return
	self.login_error("conflict","not-authorized")
 
    def irc_cmd_436(self,prefix,command,params): # ERR_NICKCOLLISION
	if self.ready:
	    return
	self.login_error("conflict","not-authorized")
 
    def irc_cmd_437(self,prefix,command,params): # ERR_UNAVAILRESOURCE
	if self.ready:
	    return
	self.login_error("resource-constraint","not-authorized")
 
    def irc_cmd_437(self,prefix,command,params): # ERR_RESTRICTED
	if self.ready:
	    return
	pass

    def irc_cmd_401(self,prefix,command,params): # ERR_NOSUCHNICK
	if len(params)>1:
	    nick,msg=params[:2]
	    error_text="%s: %s" % (nick,msg)
	else:
	    error_text=None
	self.send_error_message(params[0],"recipient-unavailable",error_text)
 
    def irc_cmd_404(self,prefix,command,params): # ERR_CANNOTSENDTOCHAN
	if len(params)>1:
	    error_text=params[1]
	else:
	    error_text=None
	self.send_error_message(params[0],"forbidden",error_text)

    def irc_cmd_QUIT(self,prefix,command,params):
	user=self.get_user(prefix)
	user.leave_all()
	self.unregister_user(user)

    def irc_cmd_352(self,prefix,command,params): # RPL_WHOREPLY
	self.debug("WHO reply received")
	if len(params)<7:
	    self.debug("too short - ignoring")
	    return
	user=self.get_user(params[4])
	user.whoreply(params)
	for c in user.channels.keys():
	    channel=user.channels[c]
	    self.component.send(channel.get_user_presence(user))
   
    def send_error_message(self,source,cond,text):
	text=remove_evil_characters(text)
   	user=self.get_user(source)
	if user:
	    self.unregister_user(user)
	if user and user.current_thread:
	    typ,thread,fr=user.current_thread
	    if not fr:
		fr=self.prefix_to_jid(source)
	    m=Message(type="error",error_cond=cond,error_text=text,
		    to=self.jid,fr=fr,thread=thread)
	else:
	    fr=self.prefix_to_jid(source)
	    m=Message(type="error",error_cond=cond,error_text=text,
		    to=self.jid,fr=fr)
	self.component.send(m)

    def pass_input_to_user(self,prefix,command,params):
	if command in self.commands_dont_show:
	    return
	nprefix=normalize(prefix)
	nnick=normalize(self.nick)
	nserver=normalize(self.server.host)
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
	fr=JID(None,self.network.jid.domain,self.server.host)
	m=Message(to=self.jid,fr=fr,body=body)
	self.component.send(m)

    def join(self,stanza):
	self.cond.acquire()
	try:
	    if not self.ready:
		self.join_requests.append(stanza)
		return
	finally:
	    self.cond.release()
	to=stanza.get_to()
	channel=node_to_channel(to.node,self.default_encoding)
	if self.channels.has_key(normalize(channel)):
	    return
	channel=Channel(self,channel)
	channel.join(stanza)
	self.channels[normalize(channel.name)]=channel

    def get_channel(self,jid):
	channel_name=jid.node
	channel_name=node_to_channel(channel_name,self.default_encoding)
	if not channel_re.match(channel_name):
	    self.debug("Bad channel name: %r" % (channel_name,))
	    return None
	return self.channels.get(normalize(channel_name))

    def message_to_channel(self,stanza):
	self.cond.acquire()
	try:
	    if not self.ready:
		self.messages_to_channel.append(stanza)
		return
	finally:
	    self.cond.release()
	channel=self.get_channel(stanza.get_to())
	if not channel:
	    e=stanza.make_error_response("bad-request")
	    self.component.send(e)
	    return
	if channel:
	    encoding=channel.encoding
	else:
	    encoding=self.default_encoding
	subject=stanza.get_subject()
	if subject and channel:
	    channel.change_topic(subject,stanza.copy())
	body=stanza.get_body()
	if body:
	    body=body.encode(encoding,"replace")
	    body=body.replace("\n"," ").replace("\r"," ")
	    if body.startswith("/me "):
		body="\001ACTION "+body[4:]+"\001"
	    self.send("PRIVMSG %s :%s" % (channel.name,body))
	    channel.irc_cmd_PRIVMSG(self.nick,"PRIVMSG",[channel.name,body])

    def message_to_user(self,stanza):
	self.cond.acquire()
	try:
	    if not self.ready:
		self.messages_to_user.append(stanza)
		return
	finally:
	    self.cond.release()
	to=stanza.get_to()
	if to.resource and (to.node[0] in "#+!" or to.node.startswith(",amp,")):
	    nick=to.resource
	    thread_fr=stanza.get_to()
	else:
	    nick=to.node
	    thread_fr=None
	nick=node_to_nick(nick,self.default_encoding,self.network)
	if not self.network.valid_nick(nick):
	    debug("Bad nick: %r" % (nick,))
	    return
	user=self.get_user(nick)
	user.current_thread=stanza.get_type(),stanza.get_thread(),thread_fr
	body=stanza.get_body().encode(self.default_encoding,"replace")
	body=body.replace("\n"," ").replace("\r"," ")
	if body.startswith("/me "):
	    body="\001ACTION "+body[4:]+"\001"
	self.send("PRIVMSG %s :%s" % (nick,body))

    def disconnect(self,reason):
	if not reason:
	    reason="Unknown reason"
	self.send("QUIT :%s" % (reason,))
	self.exit=reason
	self.exited=1

    def debug(self,msg):
	self.component.debug(msg)
    
    def print_exception(self):
	self.component.print_exception()

class ConnectionInfo:
    def __init__(self,socket,user):
	self.localip,self.localport=socket.getsockname()
	self.remoteip,self.remoteport=socket.getpeername()
	self.user=user

class SPIdentD:
    def __init__(self,component,config):
	self.socket_path=config.socket
	self.component=component
	self.socket=None
	self.queue=Queue.Queue(100)
	self.thread=threading.Thread(target=self.run_thread)
	self.thread.setDaemon(1)
	self.thread.start()

    def run_thread(self):
	while not self.component.shutdown:
	    self.socket=socket.socket(socket.AF_UNIX)
	    try:
		try:
		    self.socket.connect(self.socket_path)
		    self.loop()
		except socket.error:
		    self.print_exception()
		    pass
	    finally:
		try:
		    self.socket.close()
		except:
		    pass
		self.socket=None
		if not self.component.shutdown:
		    print >>sys.stderr,"Waiting before spidentd connection restart..."
		    time.sleep(10)

    def loop(self):
	while not self.component.shutdown:
	    try:
		item=self.queue.get(1,1)
	    except Queue.Empty:
		continue
	    while item:
		try:
		    if item[0]=="add":
			ci=item[1]
			self.socket.send("add %s:%i %s:%i %s\n" % (
			    ci.localip,ci.localport,ci.remoteip,ci.remoteport,ci.user))
		    elif item[0]=="remove":
			ci=item[1]
			self.socket.send("remove %s:%i %s:%i\n" % (
			    ci.localip,ci.localport,ci.remoteip,ci.remoteport))
		except socket.error:
		    self.queue.put(item)
		    raise
		try:
		    item=self.queue.get(0)
		except Queue.Empty:
		    break

    def register_connection(self,conninfo):
	self.queue.put(("add",conninfo))

    def unregister_connection(self,conninfo):
	self.queue.put(("remove",conninfo))

    def debug(self,msg):
	self.component.debug(msg)
    
    def print_exception(self):
	self.component.print_exception()

class Component(pyxmpp.jabberd.Component):
    def __init__(self,config):
	pyxmpp.jabberd.Component.__init__(self,config.network.jid,
		config.connect.secret,config.connect.host,config.connect.port,
		category="gateway",type="irc")
	self.shutdown=0
	signal.signal(signal.SIGINT,self.signal_handler)
	signal.signal(signal.SIGPIPE,self.signal_handler)
	signal.signal(signal.SIGTERM,self.signal_handler)
	self.irc_sessions={}
	self.config=config
	if config.spidentd:
	    self.ident_handler=SPIdentD(self,config.spidentd)
	else:
	    self.ident_handler=None

    def signal_handler(self,signum,frame):
	self.debug("Signal %i received, shutting down..." % (signum,))
	self.shutdown=1

    def run(self,timeout):
	self.connect()
	while (not self.shutdown and self.stream 
		and not self.stream.eof and self.stream.socket is not None):
	    self.stream.loop_iter(timeout)
	if self.shutdown:
	    for sess in self.irc_sessions.values():
		sess.disconnect("JJIGW shutdown")
	threads=threading.enumerate()
	for th in threads:
	    try:
		th.join(10*timeout)
	    except:
		pass
	for th in threads:
	    try:
		th.join(timeout)
	    except:
		pass
	self.disconnect()
	self.debug("Exitting normally")

    def send(self,stanza):
	self.get_stream().send(stanza)

    def stream_state_changed(self,state,arg):
	print "*** State changed: %s %r ***" % (state,arg)

    def authenticated(self):
	pyxmpp.jabberd.Component.authenticated(self)
	self.stream.set_iq_get_handler("query","jabber:iq:version",self.get_version)
	self.stream.set_iq_get_handler("query","jabber:iq:register",self.get_register)
	self.stream.set_iq_set_handler("query","jabber:iq:register",self.set_register)
	self.stream.set_iq_set_handler("query",MUC_ADMIN_NS,self.set_muc_admin)
	self.disco_info.add_feature("jabber:iq:version")
	self.disco_info.add_feature("jabber:iq:register")
	self.disco_info.add_feature(MUC_NS)
	self.stream.set_presence_handler("available",self.presence_available)
	self.stream.set_presence_handler("unavailable",self.presence_unavailable)
	self.stream.set_presence_handler("subscribe",self.presence_control)
	self.stream.set_message_handler("groupchat",self.groupchat_message)
	self.stream.set_message_handler("normal",self.message)

    def set_muc_admin(self,iq):
	to=iq.get_to()
	fr=iq.get_from()
	if not to.node:
	    self.debug("admin request sent to JID without a node")
	    iq=iq.make_error_response("feature-not-implemented")
	    self.stream.send(iq)
	    return 1
	if to.resource or not (to.node[0] in "#+!" or to.node.startswith(",amp,")):
	    self.debug("admin request sent not to a channel")
	    iq=iq.make_error_response("not-acceptable")
	    self.stream.send(iq)
	    return 1
	    
	iq=MucIq(iq)
	sess=self.irc_sessions.get(fr.as_unicode())
	if not sess:
	    self.debug("User session not found")
	    iq=iq.make_error_response("recipient-unavailable")
	    self.stream.send(iq)
	    return 1

	channel=sess.get_channel(to)
	if not channel:
	    self.debug("Channel not found")
	    iq=iq.make_error_response("recipient-unavailable")
	    self.stream.send(iq)
	    return 1

	query=iq.get_muc_child()
	if not isinstance(query,MucAdminQuery):
	    self.debug("Bad query content")
	    iq=iq.make_error_response("bad-request")
	    self.stream.send(iq)
	    return 1

	items=query.get_items()
	if not items:
	    self.debug("No items in query")
	    iq=iq.make_error_response("bad-request")
	    self.stream.send(iq)
	    return 1
	item=items[0] 
	if item.role=="none":
	    channel.kick_user(item.nick,item.reason,iq)
	elif item.role=="moderator":
	    channel.op_user(item.nick,iq)
	else:
	    self.debug("Unknown admin action")
	    iq=iq.make_error_response("feature-not-implemented")
	    self.stream.send(iq)
	    return 1
 
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

    def message(self,stanza):
	to=stanza.get_to()
	fr=stanza.get_from()
	typ=stanza.get_type()
	if typ not in (None,"chat"):
	    typ=None
	sess=self.irc_sessions.get(fr.as_unicode())
	if not to.node:
	    if sess:
		m=Message(to=fr,fr=to,body="Connected to: %s" % (sess.server,),type=typ)
	    else:
		m=Message(to=fr,fr=to,body="Not connected",type=typ)
	    return 1
	if not to.resource and (to.node[0] in "#+!" or to.node.startswith(",amp,")):
	    self.groupchat_message(stanza)
	sess=self.irc_sessions.get(fr.as_unicode())
	if sess:
	    sess.message_to_user(stanza)
	else:
	    m=stanza.make_error_response("recipient-unavailable")
	    self.send(m)
	return 1


    def groupchat_message(self,stanza):
	to=stanza.get_to()
	if not to.node:
	    self.debug("No node in groupchat message target")
	    return 0
	if to.node[0] not in "#+!" and not to.node.startswith(",amp,"):
	    self.debug("Groupchat message target is not a channel")
	    return self.message(stanza)
	if to.resource:
	    self.debug("Groupchat message target is not bare JID")
	    return 0
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

    def register_connection(self,conninfo):
	if self.ident_handler:
	    self.ident_handler.register_connection(conninfo)

    def unregister_connection(self,conninfo):
	if self.ident_handler:
	    self.ident_handler.unregister_connection(conninfo)

try:
    config=Config("jjigw.xml")

    print "creating component..."
    c=Component(config)

    print "starting..."
    c.run(1)
except JJIGWFatalError,e:
    print e
    print "Aborting."
    sys.exit(1)

# vi: sw=4 ts=8 sts=4
