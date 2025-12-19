# To cross compile:
#
# Set up as usual for bitbake:
# $ . setup-environment build
#
# In the build directory:
# $ bitbake meta-ide-support
# $ . tmp/environment-setup-cortexa9hf-vfp-neon-poky-linux-gnueabi
#
# Now a make in this directory should work.

PKG_CONFIG ?= pkg-config
PKGCONFIG = `$(PKG_CONFIG) gstreamer-1.0 --cflags --libs`

CFLAGS += $(PKGCONFIG)
CFLAGS += -Wall

all: out

out:
	$(CC) $(CFLAGS) $(LDFLAGS) hdmiout.c -o hdmiout

clean:
	$(RM) *.o *~ hdmiout

BASE := ../../..

fmt:
	@python $(BASE)/tools/build/clang-format-run.py --apply

fmt-diff:
	@python $(BASE)/tools/build/clang-format-run.py

.PHONY: all clean fmt fmt-diff
