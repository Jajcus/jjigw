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


import threading
import socket
import md5
import select
import string
import random

from pyxmpp import Message,Presence,JID
from pyxmpp.jabber.muc import MucPresence

from ircuser import IRCUser
from channel import Channel
from common import ConnectionInfo
from common import node_to_channel,normalize
from common import remove_evil_characters,strip_colors
from common import channel_re,numeric_re

class IRCSession:
    commands_dont_show=[]
    def __init__(self,component,config,netjid,jid,nick):
        self.component=component
        self.config=config
        self.network=config.get_network(netjid)
        self.default_encoding=self.network.default_encoding
        self.conninfo=None
        nick=nick.encode(self.default_encoding,"strict")
        if not self.network.valid_nick(nick):
            raise ValueError,"Bad nickname"
        self.jid=jid
        self.nick=nick
        if self.component.profile:
            ttarget=self.thread_run_prof
        else:
            ttarget=self.thread_run
        self.thread=threading.Thread(name=u"%s on %s as %s" % (jid,self.network.jid,nick),
                target=ttarget)
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
        self.login_requests=[]
        self.join_requests=[]
        self.messages_to_channel=[]
        self.messages_to_user=[]
        self.ready=0
        self.channels={}
        self.users={}
        self.raw_channel=0
        self.user=self.get_user(nick)
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
        if not self.network.valid_nick(nick,0):
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
        
    def thread_run_prof(self):
        import profile
        p=profile.Profile()
        p.runcall(self.thread_run)
        p.create_stats()
        p.dump_stats("jjigw-%s.prof" % (threading.currentThread().getName().replace("/","_"),))

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
            self.component.unregister_session(self)
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
                if sock is None:
                    self.debug("sock is None")
                    continue
                self.lock.release()
                try:
                    id,od,ed=select.select([sock],[],[sock],1)
                finally:
                    self.lock.acquire()
                if self.socket in id:
                    r=self.socket.recv(1024)
                    if r:
                        self.input_buffer+=r
                        while self.input_buffer.find("\r\n")>-1:
                            input,self.input_buffer=self.input_buffer.split("\r\n",1)
                            self._safe_process_input(input)
                    else:
                        try:
                            self.socket.close()
                        except:
                            pass
                        self.socket=None
                        self.exited=1
                elif self.socket in ed:
                    try:
                        self.socket.close()
                    except:
                        pass
                    self.socket=None
                    self.exited=1
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
        if self.raw_channel:
            self.pass_message_to_raw_channel("Connecting to %s:%s..." % (server.host,server.port))
        try:
            self.socket=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
            self.socket.connect((server.host,server.port))
        except (IOError,OSError,socket.error),err:
            self.debug("Server connect error: %r" % (err,))
            if self.raw_channel:
                self.pass_message_to_raw_channel("Connect error: %r" % (err,))
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
        if self.raw_channel:
            self.pass_message_to_raw_channel("Connected.")
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
            if self.raw_channel:
                self.pass_output_to_raw_channel(str)
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
        if self.raw_channel:
            self.pass_input_to_raw_channel(prefix,command,params)
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
        if len(params)<2 or not prefix:
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
                for s in self.join_requests+self.login_requests:
                    p=s.make_error_response(join_condition)
                    self.component.send(p)
                    try:
                        self.used_for.remove(s.get_to())
                    except ValueError:
                        pass
                self.join_requests=[]
                self.login_requests=[]
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
            for s in self.login_requests:
                self.login(s)
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
        if not user:
            self.debug("User: %r not found" % (params[4],))
        else:
            user.whoreply(params)

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

    def pass_input_to_raw_channel(self,prefix,command,params):
        body=string.join([command]+params)
        body=`body`
        if body[0] in '"\'':
            body=body[1:-1]
        body=unicode(body,self.default_encoding,"replace")
        if prefix:
            prefix=remove_evil_characters(prefix)
            prefix=`prefix`
            if prefix[0] in '"\'':
                prefix=prefix[1:-1]
            prefix=unicode(prefix,self.default_encoding,"replace")
        else:
            prefix=None
        fr=JID('#',self.network.jid.domain,prefix)
        m=Message(to=self.jid,fr=fr,body=body,type="groupchat")
        self.component.send(m)

    def pass_output_to_raw_channel(self,s):
        body=`s`
        if body[0] in '"\'':
            body=body[1:-1]
        body=unicode(body,self.default_encoding,"replace")
        nick=unicode(self.nick,self.default_encoding,"replace")
        fr=JID('#',self.network.jid.domain,nick)
        m=Message(to=self.jid,fr=fr,body=body,type="groupchat")
        self.component.send(m)

    def pass_message_to_raw_channel(self,msg):
        fr=JID('#',self.network.jid.domain,None)
        m=Message(to=self.jid,fr=fr,body=msg,type="groupchat")
        self.component.send(m)

    def join(self,stanza):
        to=stanza.get_to()
        if to.node=='#':
            return self.join_raw_channel(stanza)
        self.cond.acquire()
        try:
            if not self.ready:
                self.join_requests.append(stanza)
                return
        finally:
            self.cond.release()
        try:
            channel=node_to_channel(to.node,self.default_encoding)
        except ValueError:
            e=stanza.make_error_response("not-acceptable")
            self.component.send(e)
            return
        if self.channels.has_key(normalize(channel)):
            return
        if to not in self.used_for:
            self.used_for.append(to)
        channel=Channel(self,channel)
        channel.join(stanza)
        self.channels[normalize(channel.name)]=channel

    def join_raw_channel(self,stanza):
        self.raw_channel=1
        to=stanza.get_to()
        if to not in self.used_for:
            self.used_for.append(to)
        p=Presence(fr=to,to=stanza.get_from())
        self.component.send(p)

    def leave(self,stanza):
        to=stanza.get_to()
        if to.node=='#':
            return self.leave_raw_channel(stanza)
        channel=self.get_channel(stanza.get_to())
        if channel:
           channel.leave(stanza)
           self.logout(stanza,0)
        else:
           self.logout(stanza)

    def leave_raw_channel(self,stanza):
        self.raw_channel=0
        self.logout(stanza)

    def login(self,stanza):
        self.cond.acquire()
        try:
            if not self.ready:
                self.login_requests.append(stanza)
                return
        finally:
            self.cond.release()
        to=stanza.get_to()
        if to not in self.used_for:
            self.used_for.append(to)
        fr=stanza.get_from()
        p=Presence(to=fr,fr=to,status=stanza.get_status(),show=stanza.get_show())
        self.component.send(p)

    def logout(self,stanza,send_response=1):
        to=stanza.get_to()
        if to not in self.used_for:
            self.debug("Unavailable presence sent with no matching available presence, ignoring it")
            return 0
        try:
            self.used_for.remove(to)
        except:
            pass
        if send_response:
            p=Presence(
                type="unavailable",
                to=stanza.get_from(),
                fr=stanza.get_to()
                );
            self.component.send(p)
        if not self.used_for:
            self.disconnect(stanza.get_status())
            return 1
        else:
            return 0

    def channel_left(self,channel):
        try:
            del self.channels[normalize(channel.name)]
        except KeyError:
            pass
        if not channel.room_jid:
            return
        if channel.room_jid not in self.used_for:
            return
        try:
            self.used_for.remove(channel.room_jid)
        except:
            pass
        if not self.used_for:
            self.disconnect("Quit")

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

# vi: sts=4 et sw=4
