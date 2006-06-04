#!/usr/bin/python -u
#
#  Jajcus' Jabber to IRC Gateway
#  Copyright (C) 2004  Jacek Konieczny <jajcus@jajcus.net>
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

import Queue
import threading
import socket
import logging

class SPIdentD:
    def __init__(self,component,config):
        self.__logger=logging.getLogger("jjigw.SPIdentD")
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
                    self.__logger.exception("Exception cought for path \"%s\":" % (self.socket_path))
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

# vi: sts=4 et sw=4
