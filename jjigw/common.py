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


import re
import string
from types import StringType,UnicodeType

class JJIGWFatalError(RuntimeError):
    pass

evil_characters_re=re.compile(r"[\000-\010\013\014\016-\037]")
def remove_evil_characters(s):
    return evil_characters_re.sub(" ",s)

color_re=re.compile(r"\x03\d\d|\x0f")
def strip_colors(s):
    return color_re.sub("",s)

numeric_re=re.compile(r"\d\d\d")
channel_re=re.compile(r"^[&#+!][^\000 \007 ,:\r\n]+$")
nick_re=re.compile(r"^[a-zA-Z\x5b-\x60\x7b-\x7d\[\]\\`_^{|}][a-zA-Z\x5b-\x60\x7b-\x7d\[\]\\`_^{|}0-9-]*$")
nick8_re=re.compile(r"^[a-zA-Z\x5b-\x60\x7b-\x7d\[\]\\`_^{|}\x80-\xff][a-zA-Z\x5b-\x60\x7b-\x7d\[\]\\`_^{|}0-9\x80-\xff-]*$")

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

class ConnectionInfo:
    def __init__(self,socket,user):
        self.localip,self.localport=socket.getsockname()
        self.remoteip,self.remoteport=socket.getpeername()
        self.user=user


# vi: sts=4 et sw=4
