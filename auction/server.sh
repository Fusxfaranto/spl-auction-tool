#!/bin/sh

gunicorn -k flask_sockets.worker -b '0.0.0.0:4000' __init__:app
