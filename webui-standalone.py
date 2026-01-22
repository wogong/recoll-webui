#!/usr/bin/env python3
import os
import argparse
import webui

# handle command-line arguments
parser = argparse.ArgumentParser()
parser.add_argument('-a', '--addr', default='127.0.0.1',help='address to bind to [127.0.0.1]')
parser.add_argument('-p', '--port', default='8080', type=int, help='port to listen on [8080]')
parser.add_argument('-c', '--config', action='append', help='configuration directory (primary) or extra indices')
args = parser.parse_args()

if args.config:
    # First -c is the primary config dir
    os.environ["RECOLL_CONFDIR"] = args.config[0]
    # Additional -c args are extra config dirs for searching multiple indices
    if len(args.config) > 1:
        os.environ["RECOLL_EXTRACONFDIRS"] = ' '.join(args.config[1:])

# change to webui's directory and import
if os.path.dirname(__file__) != "":
    os.chdir(os.path.dirname(__file__))

# set up webui and run in own http server
webui.bottle.debug(True)
webui.bottle.run(server='waitress', host=args.addr, port=args.port)

# vim: foldmethod=marker:filetype=python:textwidth=80:ts=4:et
