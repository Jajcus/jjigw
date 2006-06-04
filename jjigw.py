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

import sys
import os.path
import logging

from jjigw.common import JJIGWFatalError
from jjigw.config import Config
from jjigw.component import Component

def main(profile=False):
    config_dir,data_dir=".","."

    try:
        logger=logging.getLogger()
        logger.addHandler(logging.StreamHandler())
        logger.setLevel(logging.DEBUG)
        try:
            config=Config(config_dir,data_dir)
        except:
            print >>sys.stderr,"Couldn't load config file:",str(sys.exc_value)
            sys.exit(1)

        print "creating component..."
        c=Component(config,profile=profile)

        print "starting..."
        c.run(1)
    except JJIGWFatalError,e:
        print e
        print "Aborting."
        sys.exit(1)

if '--profile' in sys.argv:
    import profile
    sys.argv.remove('--profile')
    profile.run("main(profile=True)","jjigw.prof")
else:
    main()

# vi: sts=4 et sw=4
