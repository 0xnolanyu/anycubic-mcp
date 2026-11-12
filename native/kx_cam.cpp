// kx_cam.cpp — Agora Windows C++ 拉流截帧器
// 用法: kx_cam.exe <config.json>  (字段: appid, token, channel, uid, key, salt_b64, out, wait_secs)
#include <AgoraBase.h>
#include <IAgoraRtcEngine.h>
#include <IAgoraMediaEngine.h>
#include <windows.h>
#include <objidl.h>
#include <gdiplus.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <atomic>
#pragma comment(lib, "gdiplus.lib")
using namespace agora;
using namespace agora::media;
using namespace agora::rtc;

static std::atomic<bool> g_done{false};
static std::atomic<int> g_frameCount{0};
static char g_out[MAX_PATH];

static void b64_decode(const char* in, uint8_t* out, int outCap) {
  static const char* T = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  int vals[256]; for (int i=0;i<256;i++) vals[i]=-1;
  for (int i=0;i<64;i++) vals[(unsigned char)T[i]]=i;
  int len = (int)strlen(in), o=0;
  for (int i=0;i<len && o<outCap;) {
    int v[4]={-1,-1,-1,-1}; int n=0;
    while (i<len && n<4) { int d=vals[(unsigned char)in[i]]; if (d>=0) v[n++]=d; i++; if (in[i-1]=='=') break; }
    if (n>=2 && o<outCap) out[o++]=(uint8_t)((v[0]<<2)|((v[1]&0x30)>>4));
    if (n>=3 && o<outCap) out[o++]=(uint8_t)(((v[1]&0xF)<<4)|((v[2]&0x3C)>>2));
    if (n>=4 && o<outCap) out[o++]=(uint8_t)(((v[2]&0x3)<<6)|v[3]);
  }
}

// 简易 JSON 字符串字段提取
static bool json_str(const char* js, const char* key, char* out, int cap) {
  char pat[64]; snprintf(pat,sizeof(pat),"\"%s\"",key);
  const char* p = strstr(js,pat); if (!p) return false;
  p = strchr(p+strlen(pat),':'); if (!p) return false;
  p = strchr(p+1,'"'); if (!p) return false;
  const char* e = strchr(p+1,'"'); if (!e) return false;
  int n=(int)(e-p-1); if (n>=cap) n=cap-1;
  memcpy(out,p+1,n); out[n]=0; return true;
}
static long json_long(const char* js, const char* key, long dflt) {
  char pat[64]; snprintf(pat,sizeof(pat),"\"%s\"",key);
  const char* p=strstr(js,pat); if(!p) return dflt;
  p=strchr(p+strlen(pat),':'); if(!p) return dflt;
  return atol(p+1);
}

static int GetJpegClsid(GUID* pguid) {
  UINT n=0, sz=0; Gdiplus::GetImageEncodersSize(&n,&sz);
  if (!sz) return -1;
  Gdiplus::ImageCodecInfo* p=(Gdiplus::ImageCodecInfo*)malloc(sz);
  Gdiplus::GetImageEncoders(n,sz,p);
  for (UINT i=0;i<n;i++)
    if (wcscmp(p[i].MimeType,L"image/jpeg")==0){*pguid=p[i].Clsid;free(p);return (int)i;}
  free(p); return -1;
}

class FrameObs : public IVideoFrameObserver {
public:
  bool onRenderVideoFrame(const char* ch, rtc::uid_t uid_, VideoFrame& vf) override {
    if (g_done.load()) return false;
    if (vf.type != agora::media::base::VIDEO_PIXEL_I420) { // 只处理 I420
      return false;
    }
    int w=vf.width,h=vf.height;
    if (w<=0||h<=0) return false;
    unsigned char* rgb=(unsigned char*)malloc((size_t)w*h*3);
    const unsigned char* Y=vf.yBuffer,*U=vf.uBuffer,*V=vf.vBuffer;
    int ys=vf.yStride, cs=vf.uStride;
    for (int y=0;y<h;y++){
      for (int x=0;x<w;x++){
        int yy=Y[y*ys+x];
        int uu=U[(y/2)*cs+(x/2)], vv=V[(y/2)*cs+(x/2)];
        int r=yy+1.402f*(vv-128), g=yy-0.344f*(uu-128)-0.714f*(vv-128), b=yy+1.772f*(uu-128);
        unsigned char* d=rgb+3*((size_t)y*w+x);
        d[0]=r<0?0:r>255?255:r; d[1]=g<0?0:g>255?255:g; d[2]=b<0?0:b>255?255:b;
      }
    }
    Gdiplus::Bitmap bmp(w,h,3*w,PixelFormat24bppRGB,rgb);
    GUID jpg; if (GetJpegClsid(&jpg)>=0) {
      wchar_t wout[MAX_PATH]; MultiByteToWideChar(CP_UTF8,0,g_out,-1,wout,MAX_PATH);
      bmp.Save(wout,&jpg,NULL);
    }
    free(rgb);
    g_frameCount++;
    if (g_frameCount>=3) g_done.store(true); // 收满 3 帧即退
    return false;
  }
  bool onCaptureVideoFrame(rtc::VIDEO_SOURCE_TYPE, VideoFrame&) override { return false; }
  bool onMediaPlayerVideoFrame(VideoFrame&, int) override { return false; }
  bool onTranscodedVideoFrame(VideoFrame&) override { return false; }
  bool onPreEncodeVideoFrame(rtc::VIDEO_SOURCE_TYPE, VideoFrame&) override { return false; }
};

class Ev : public IRtcEngineEventHandler {
public:
  void onError(int err, const char* msg) override { printf("{\"ev\":\"error\",\"code\":%d,\"msg\":\"%s\"}\n",err,msg?msg:""); }
  void onJoinChannelSuccess(const char*, uid_t, int elapsed) override { printf("{\"ev\":\"joined\",\"elapsed\":%d}\n",elapsed); }
  void onLeaveChannel(const RtcStats&) override { printf("{\"ev\":\"left\"}\n"); }
  void onUserJoined(uid_t uid, int elapsed) override { printf("{\"ev\":\"user_published\",\"uid\":%u}\n",(unsigned)uid); }
  void onConnectionStateChanged(CONNECTION_STATE_TYPE st, CONNECTION_CHANGED_REASON_TYPE r) override {
    printf("{\"ev\":\"conn\",\"state\":%d,\"reason\":%d}\n",(int)st,(int)r);
  }
};

int main(int argc, char** argv) {
  if (argc<2) { printf("usage: kx_cam <config.json>\n"); return 2; }
  FILE* f=fopen(argv[1],"rb"); if(!f){printf("no config\n");return 2;}
  static char js[8192]; size_t n=fread(js,1,sizeof(js)-1,f); js[n]=0; fclose(f);
  char appid[128],token[512],channel[128],key[128],saltb64[128];
  if(!json_str(js,"appid",appid,sizeof(appid))||!json_str(js,"token",token,sizeof(token))||
     !json_str(js,"channel",channel,sizeof(channel))||!json_str(js,"key",key,sizeof(key))||
     !json_str(js,"salt_b64",saltb64,sizeof(saltb64))||!json_str(js,"out",g_out,sizeof(g_out))) {
    printf("config missing field\n"); return 2;
  }
  long uid=json_long(js,"uid",0), wait=json_long(js,"wait_secs",40);
  uint8_t salt[32]={0}; b64_decode(saltb64,salt,32);

  Gdiplus::GdiplusStartupInput si; ULONG_PTR gdp;
  Gdiplus::GdiplusStartup(&gdp,&si,NULL);

  RtcEngineContext ctx; Ev ev;
  ctx.appId=appid; ctx.eventHandler=&ev;
  ctx.channelProfile=CHANNEL_PROFILE_LIVE_BROADCASTING;
  IRtcEngine* eng=createAgoraRtcEngine();
  if (eng->initialize(ctx)) { printf("init fail\n"); return 1; }
  eng->enableVideo();

  FrameObs obs;
  agora::util::AutoPtr<IMediaEngine> media;
  media.queryInterface(eng,AGORA_IID_MEDIA_ENGINE);
  media->registerVideoFrameObserver(&obs);

  EncryptionConfig ec;
  ec.encryptionMode=AES_256_GCM2; ec.encryptionKey=key;
  memcpy(ec.encryptionKdfSalt,salt,32);
  eng->enableEncryption(true,ec);

  ChannelMediaOptions opt;
  opt.autoSubscribeAudio=false; opt.autoSubscribeVideo=true;
  opt.clientRoleType=CLIENT_ROLE_AUDIENCE;
  int rc=eng->joinChannel(token,channel,(uid_t)uid,opt);
  printf("{\"ev\":\"join_rc\",\"rc\":%d}\n",rc);

  DWORD t0=GetTickCount();
  while (!g_done.load() && GetTickCount()-t0 < (DWORD)wait*1000) Sleep(200);
  eng->leaveChannel();
  Sleep(300);
  media->registerVideoFrameObserver(nullptr);
  eng->release();
  Gdiplus::GdiplusShutdown(gdp);
  printf("{\"ev\":\"done\",\"frames\":%d}\n",g_frameCount.load());
  return g_frameCount.load()>0?0:1;
}
