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


from pyxmpp import JID

from common import normalize,nick_to_node

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
        if self.user and self.host:
            res=unicode(self.user+'@'+self.host,self.session.default_encoding,"replace")
        else:
            res=u""
        return JID(nick_to_node(self.nick,self.session.default_encoding),
                self.session.network.jid.domain,res)

    def __repr__(self):
        return "<IRCUser %r: %r>" % (id(self),self.nick)

    def debug(self,msg):
        return self.session.debug(msg)

# vi: sts=4 et sw=4
