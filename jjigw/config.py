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


import libxml2
from pyxmpp import JID
from common import JJIGWFatalError,nick8_re,nick_re,normalize
import os

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
    def __repr__(self):
        return "<ServerConfig %s:%s/>" % (self.host,self.port)

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
        self.name=node.prop("name")
        self.max_nick_length=int(node.prop("max_nick_length"))
        self.max_channel_length=int(node.prop("max_nick_length"))
    def get_servers(self):
        r=self.servers
        self.servers=self.servers[-1:]+self.servers[1:]
        return r
    def get_channel_config(self,channel):
        return self.channels.get(normalize(channel))
    def valid_nick(self,s,strict=1):
        if self.nicks_8bit:
            m=nick8_re.match(s)
        else:
            m=nick_re.match(s)
        if not m:
            return 0
        if not strict:
            return 1
        if len(s)<=self.max_nick_length:
            return 1
        return 0

class Config:
    def __init__(self,config_dir,data_dir):
        self.doc=None
        self.config_dir=config_dir
        self.data_dir=data_dir
        os.chdir(data_dir)
        libxml2.initializeCatalog()
        libxml2.loadCatalog(os.path.join(data_dir,"catalog.xml"))
        parser=libxml2.createFileParserCtxt(os.path.join(config_dir,"jjigw.xml"))
        parser.validate(1)
        parser.parseDocument()
        if not parser.isValid():
            raise JJIGWFatalError,"Invalid configuration"
        self.doc=parser.doc()
        self.connect=ConnectConfig(self.doc.xpathEval("jjigw/connect")[0])
        self.jid=None
        self.networks={}
        for n in self.doc.xpathEval("jjigw/network"):
            network=NetworkConfig(n)
            if not self.jid:
                self.jid=network.jid
            self.networks[network.jid.domain]=network
        spidentd=self.doc.xpathEval("jjigw/spidentd")
        if spidentd:
            self.spidentd=SPIdentDConfig(spidentd[0])
        else:
            self.spidentd=None
        self.admins=[]
        for n in self.doc.xpathEval("jjigw/admin"):
            self.admins.append(JID(n.getContent()))
    def get_network(self,jid):
        if isinstance(jid,JID):
            return self.networks[jid.domain]
        else:
            return self.networks[jid]
    def __del__(self):
        if self.doc:
            self.doc.freeDoc()

# vi: sts=4 et sw=4
