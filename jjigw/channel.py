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

import string
import logging

from pyxmpp import Message,JID
from pyxmpp.jabber.muc import MucPresence,MucItem,MucStatus

from requests import Request,RequestQueue
from common import channel_re
from common import normalize,remove_evil_characters,strip_colors
from ircuser import IRCUser

class Channel:
    toggle_modes="aimnqpsrt"
    arg_modes="kl"
    multiarg_modes="OovbeI"
    def __init__(self,session,name):
        self.__logger=logging.getLogger("jjigw.Channel")
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
        self.users=[self.session.user]
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
        m=Message(from_jid=self.room_jid.bare(),to_jid=self.session.jid,stanza_type="groupchat",body=msg)
        self.session.component.send(m)

    def join(self,stanza):
        if self.state:
            self.__logger.debug("Channel %r not in the initial state, not joining!" % (self.name,))
            p=stanza.make_error_response(stanza,"bad-request")
            self.session.component.send(p)
            return
        self.room_jid=stanza.get_to()
        self.__logger.debug("Joining channel %r" % (self.name,))
        self.session.send("JOIN %s" % (self.name,))
        self.requests.add("JOIN",stanza)
        self.state="join"
        if stanza.get_join_info():
            self.muc=1

    def leave(self,stanza):
        status=stanza.get_status()
        if not self.state:
            self.__logger.debug("Channel %r in the initial state - nothing to do." % (self.name,))
        else:
            if not status:
                self.session.send("PART %s" % (self.name,))
            else:
                self.session.send("PART %s :%s" % (self.name,
                        status.encode(self.encoding,"replace")))
            self.state=None
        p=MucPresence(stanza_type="unavailable",from_jid=stanza.get_to(),to_jid=stanza.get_from(),status=status)
        self.session.component.send(p)
        for u in self.users:
            u.leave_channel(self)
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
            p=MucPresence(from_jid=self.nick_to_jid(user.nick),to_jid=self.session.jid)
        else:
            p=MucPresence(stanza_type="unavailable",from_jid=self.nick_to_jid(user.nick),to_jid=self.session.jid)
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
        self.__logger.debug("Nick changed: %r -> %r" % (oldnick,user.nick))
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
        # this often is not an error
        pass
        #self.irc_error_response(prefix,command,params,["TOPIC","MODE"],"not-acceptable")

    def irc_error_response(self,prefix,command,params,requests,condition):
        r=self.requests.get(requests)
        if r:
            m=r.stanza.make_error_response(condition)
        else:
            m=Message(from_jid=self.room_jid.bare(),to_jid=self.session.jid,
                    stanza_type="error", error_cond=condition)
        self.session.component.send(m)

    def irc_cmd_331(self,prefix,command,params): # RPL_NOTOPIC
        m=Message(from_jid=self.room_jid.bare(),to_jid=self.session.jid, stanza_type="groupchat", subject=u"")
        self.session.component.send(m)

    def irc_cmd_332(self,prefix,command,params): # RPL_TOPIC
        topic=remove_evil_characters(params[1])
        m=Message(from_jid=self.room_jid.bare(),to_jid=self.session.jid,
                stanza_type="groupchat", subject=unicode(topic,self.encoding,"replace"))
        self.session.component.send(m)

    def irc_cmd_TOPIC(self,prefix,command,params):
        self.requests.get("TOPIC")
        topic=remove_evil_characters(params[1])
        m=Message(from_jid=self.prefix_to_jid(prefix),to_jid=self.session.jid,
                stanza_type="groupchat", subject=unicode(topic,self.encoding,"replace"))
        self.session.component.send(m)

    def irc_cmd_MODE(self,prefix,command,params):
        if len(params)<2:
            self.__logger.debug("No parameters in received MODE")
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
            r=self.requests.get("MODE",string.join(params[1:]," "))
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
                self.__logger.debug("Not '+' or '-' before '%s' in received MODE" % (m,))
                continue
            elif m in self.arg_modes or m in self.multiarg_modes:
                if not len(params):
                    self.__logger.debug("No argument for mode '%s' in received MODE" % (m,))
                    continue
                arg=params.pop(0)
            elif m in self.toggle_modes:
                arg=None
            else:
                self.__logger.debug("Unknown mode '%s' in received MODE" % (m,))
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
        self.__logger.debug("Mode changed: %r by %r" % (user.nick,actor_jid))
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
                self.__logger.debug("Channel %r joined!" % (self.name,))
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
        if self.session.check_prefix(prefix):
            self.session.channel_left(self)
        else:
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
        if self.session.check_nick(params[1]):
            self.session.channel_left(self)

    def irc_cmd_PRIVMSG(self,prefix,command,params):
        self.irc_message(prefix,command,params)

    def irc_cmd_NOTICE(self,prefix,command,params):
        self.irc_message(prefix,command,params)

    def irc_message(self,prefix,command,params):
        if not self.state or len(params)<2:
            self.__logger.debug("ignoring it")
            return
        body=unicode(params[1],self.encoding,"replace")
        if body[0]=="\001" and body[-1]=="\001":
            self.CTCP(prefix,body[1:-1])
        else:
            m=Message(stanza_type="groupchat",from_jid=self.prefix_to_jid(prefix),to_jid=self.session.jid,
                    body=remove_evil_characters(strip_colors(body)))
            self.session.component.send(m)

    def CTCP(self,prefix,command):
        if " " in command:
            command,arg=command.split(" ",1)
        else:
            arg=None
        if command=="ACTION":
            m=Message(stanza_type="groupchat",from_jid=self.prefix_to_jid(prefix),to_jid=self.session.jid,
                    body="/me "+remove_evil_characters(strip_colors(arg)))
            self.session.component.send(m)
        else:
            self.__logger.debug("Unknown CTCP command: %r %r" % (command,arg))

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
        if user in self.modes.get("v",[]):
            change="-v+o %s %s" % (nick,nick)
        else:
            change="+o "+nick
        self.session.send("MODE %s %s" % (self.name,change))
        self.requests.add("MODE",stanza,change)

    def voice_user(self,nick,stanza):
        nick=nick.encode(self.encoding,"strict")
        user=self.session.users.get(normalize(nick))
        if not user in self.users:
           r=stanza.make_error_response("item-not-found")
           self.session.component.send(r)
           return
        if user in self.modes.get("v",[]):
           r=stanza.make_result_response()
           self.session.component.send(r)
           return
        if user in self.modes.get("o",[]):
            change="-o+v %s %s" % (nick,nick)
        else:
            change="+v "+nick
        self.session.send("MODE %s %s" % (self.name,change))
        self.requests.add("MODE",stanza,change)

    def devoice_user(self,nick,stanza):
        nick=nick.encode(self.encoding,"strict")
        user=self.session.users.get(normalize(nick))
        if not user in self.users:
           r=stanza.make_error_response("item-not-found")
           self.session.component.send(r)
           return
        if user in self.modes.get("v",[]) and user in self.modes.get("o",[]):
           change="-o-v %s %s" % (nick,nick)
        elif user in self.modes.get("o",[]):
            change="-o "+nick
        elif user in self.modes.get("v",[]):
            change="-v "+nick
        else:
           r=stanza.make_result_response()
           self.session.component.send(r)
           return
        self.session.send("MODE %s %s" % (self.name,change))
        self.requests.add("MODE",stanza,change)

    def __repr__(self):
        return "<IRCChannel %r>" % (self.name,)

# vi: sts=4 et sw=4
