#!/bin/bash

RWFS=/mnt/rootfs.rw
LIBDIR=/usr/lib

if [ -e $RWFS/$LIBDIR/sndast* ] ||
   [ -e $RWFS/$LIBDIR/libvpu* ] ||
   [ -e $RWFS/$LIBDIR/libfsl* ] ||
   [ -e $RWFS/$LIBDIR/gstrea* ] ; then

  #Remove all the sculpture libraries
  rm -rf $RWFS/$LIBDIR/sndast
  rm -rf $RWFS/$LIBDIR/libvpu*
  rm -rf $RWFS/$LIBDIR/libfslvpu*
  rm -rf $RWFS/$LIBDIR/gstreamer*
  rm -rf $RWFS/$LIBDIR/.*libfslvpu*

  #Remove the gstreamer registry
  rm -f ~/.cache/gstreamer-1.0/registry.* >/dev/null 2>&1
  rm -f ~/.gstreamer-0.10/registry.arm.bin >/dev/null 2>&1

  #Rebuild the gstreamer registry
  if command -v gst-inspect-1.0 >/dev/null 2>&1; then
    gst-inspect-1.0 > /dev/null 2>&1
  else
    gst-inspect > /dev/null 2>&1
  fi

fi
