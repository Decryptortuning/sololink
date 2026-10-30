#include "Diagnostics.h"
#include "Telem.h"
#include "SLIP.h"
#include <assert.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
#include <fcntl.h>
#include <string.h>
#include <stddef.h>
#include <string>
extern "C" void opensolo_syslog(int, const char *, ...) {}
extern "C" uint64_t clock_gettime_us(clockid_t) { return 1234567; }
extern "C" const char *clock_gettime_str_r(clockid_t, char *b) { b[0]=0; return b; }
static void sendPacket(int socket, int destination, const void *bytes, unsigned n) {
    sockaddr_in address = {}; socklen_t len=sizeof(address);
    assert(!getsockname(destination,reinterpret_cast<sockaddr*>(&address),&len));
    assert(sendto(socket,bytes,n,0,reinterpret_cast<sockaddr*>(&address),len)==int(n));
}
int main() {
    int uart[2]; assert(!pipe2(uart,O_NONBLOCK));
    int client=socket(AF_INET,SOCK_DGRAM|SOCK_NONBLOCK,0); assert(client>=0);
    Diagnostics d(0); assert(d.getfd()>=0);
    sockaddr_in address={}; socklen_t addressLen=sizeof(address);
    assert(!getsockname(d.getfd(),reinterpret_cast<sockaddr*>(&address),&addressLen));
    assert(address.sin_addr.s_addr==htonl(INADDR_LOOPBACK));
    const unsigned char invalid[]={1,0xc0,0xdb,0};
    sendPacket(client,d.getfd(),invalid,4); d.downHandler(uart[1]);
    char received[1024]; assert(read(uart[0],received,sizeof(received))<0);
    sendPacket(client,d.getfd(),invalid,3); d.downHandler(uart[1]);
    int n=read(uart[0],received,sizeof(received)); assert(n>0);
    char raw[300]; SLIPDecoder decoder(raw,sizeof(raw)); int decoded=0;
    for(int i=0;i<n;++i) {int size=decoder.addByte(&received[i]); if(size>0) decoded=size;}
    assert(decoded==4 && raw[0]==27 && raw[1]==1 && (unsigned char)raw[2]==0xc0 && (unsigned char)raw[3]==0xdb);
    sendPacket(client,d.getfd(),invalid,3); d.downHandler(uart[1]);
    assert(read(uart[0],received,sizeof(received))<0); // rate limited
    char response[204]={1,0,(char)204,0,(char)0xc0,(char)0xdb};
    assert(!d.upHandler(response,203));
    response[4]=0; assert(!d.upHandler(response,204)); response[4]=(char)0xc0;
    assert(d.upHandler(response,204)==204);
    assert(recv(client,received,sizeof(received),0)==204 && !memcmp(received,response,204));
    assert(!d.upHandler(response,204)); // consumed; unsolicited repeats dropped
    usleep(1100000);
    sendPacket(client,d.getfd(),invalid,3); d.downHandler(uart[1]);
    assert(read(uart[0],received,sizeof(received))>0);
    usleep(1100000); assert(!d.upHandler(response,204)); // expired
    assert(recv(client,received,sizeof(received),0)<0);
    Diagnostics conflict(ntohs(address.sin_port)); assert(conflict.getfd()<0);
    std::string empty;
    Telem telem("127.0.0.1",0,0,0,empty);
    LinkPacket packet={};
    // Actual Telem downstream path; synthetic minimal frames test ID filtering,
    // not MAVLink CRC validity (this forwarding layer deliberately does not parse CRC).
    const unsigned ids[]={120,97,134};
    for(unsigned i=0;i<3;++i) {unsigned off=i*8; packet.payload[off]=0xfe; packet.payload[off+5]=ids[i];}
    sendPacket(client,telem.getfd(),&packet,offsetof(LinkPacket,payload)+24);
    telem.downHandler(uart[1]);
    n=read(uart[0],received,sizeof(received)); assert(n>0);
    SLIPDecoder td(raw,sizeof(raw)); unsigned count=0;
    for(int i=0;i<n;++i) {int size=td.addByte(&received[i]); if(size>0) {
        assert(size==9 && raw[0]==4); assert((unsigned char)raw[6]==ids[++count]); }}
    assert(count==2); close(client); close(uart[0]); close(uart[1]);
}
