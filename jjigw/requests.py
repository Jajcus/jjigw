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


from types import StringType,UnicodeType

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

# vi: sw=4 ts=8 sts=4
