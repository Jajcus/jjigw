#!/usr/bin/python -u
#
#  Jajcus' Jabber to IRC Gateway
#  Copyright (C) 2004  Jacek Konieczny <jajcus@bnet.pl>
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License along
#  with this program; if not, write to the Free Software Foundation, Inc.,
#  59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.


import signal
import threading

import pyxmpp.jabberd
from pyxmpp import Presence
from pyxmpp.jabber.muc import MUC_ADMIN_NS,MUC_NS
from pyxmpp.jabber.muc import MucPresence,MucIq,MucAdminQuery
from pyxmpp.jabber.disco import DiscoItems,DiscoInfo,DiscoIdentity

from ircsession import IRCSession

class Component(pyxmpp.jabberd.Component):
    def __init__(self,config):
	pyxmpp.jabberd.Component.__init__(self,config.jid,
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

    def get_session(self,user_jid,component_jid):
	print `self.irc_sessions`
	return self.irc_sessions.get((user_jid.as_unicode(),component_jid.domain))

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
	sess=self.get_session(fr,to)
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
	elif item.role=="visitor":
	    channel.devoice_user(item.nick,iq)
	elif item.role=="participant":
	    channel.voice_user(item.nick,iq)
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
	sess=self.get_session(fr,to)
	if not to.node:
	    if sess:
		m=Message(to=fr,fr=to,body="Connected to: %s" % (sess.server,),type=typ)
	    else:
		m=Message(to=fr,fr=to,body="Not connected",type=typ)
	    return 1
	if not to.resource and (to.node[0] in "#+!" or to.node.startswith(",amp,")):
	    self.groupchat_message(stanza)
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
	sess=self.get_session(fr,to)
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
	sess=self.get_session(fr,to)
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
	    sess=IRCSession(self,self.config,to,fr,nick)
	    sess.used_for.append(to)
	    self.irc_sessions[fr.as_unicode(),to.domain]=sess
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
	sess=self.get_session(fr,to)
	if sess:
	    try:
		sess.used_for.remove(to)
	    except ValueError:
		pass
	    if not sess.used_for:
		sess.disconnect(status)
		try:
		    del self.irc_sessions[fr.as_unicode(),to.domain]
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

    def disco_get_info(self,node,iq):
	to=iq.get_to()
	try:
	    network=self.config.get_network(to)
	except KeyError:
	    return iq.make_error_response("recipient-unavailable")
	if to.node is None and to.resource is None:
		di=DiscoInfo()
		di.add_feature("jabber:iq:version")
		di.add_feature("jabber:iq:register")
		di.add_feature(MUC_NS)
		if network.name:
		    name=network.name
		else:
		    name="IRC gateway"
		DiscoIdentity(di,name,"gateway","irc")
		return di
	return iq.make_error_response("feature-not-implemented")

    def disco_get_items(self,node,iq):
	to=iq.get_to()
	try:
	    network=self.config.get_network(to)
	except KeyError:
	    return iq.make_error_response("recipient-unavailable")
	if to.node is None and to.resource is None:
		return self.disco_items
	return iq.make_error_response("feature-not-implemented")

# vi: sw=4 ts=8 sts=4
