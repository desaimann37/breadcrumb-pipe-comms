/*
 * BREADCRUMB camera node - ESP32-CAM / XIAO ESP32-S3 Sense
 * ========================================================
 *
 * Serves an MJPEG stream at  http://<ip>:81/stream  and single frames at
 * http://<ip>/jpg. The operator dashboard points an <img> at /stream when
 * launched with --camera, so the same console shows real video instead of
 * the simulated feed.
 *
 * Why MJPEG and not H.264
 * -----------------------
 * MJPEG is per-frame independent. When a packet is lost you lose one frame,
 * not everything up to the next keyframe, which is exactly the failure mode
 * you want on a link whose margin is about to run out. It costs roughly 3x
 * the bitrate of H.264 for the same quality - an honest trade documented in
 * docs/HARDWARE_INTEGRATION.md, and the reason the quality ladder below
 * starts lower than the simulator's.
 *
 * The sketch exposes a quality ladder over HTTP so the bridge can step
 * resolution down as the link degrades, mirroring what pipecomm/video.py
 * does in simulation:
 *
 *     http://<ip>/tier?i=0   1600x1200  (defect grading)
 *     http://<ip>/tier?i=3   640x480    (general condition)
 *     http://<ip>/tier?i=5   320x240    (driving only)
 *
 * Board: "AI Thinker ESP32-CAM" or "XIAO_ESP32S3". Set CAMERA_MODEL below.
 */

#include "esp_camera.h"
#include <WiFi.h>
#include <esp_http_server.h>

// ======================================================= configuration ===

#define CAMERA_MODEL_AI_THINKER      // or CAMERA_MODEL_XIAO_ESP32S3

const char *AP_SSID = "breadcrumb-cam";
const char *AP_PASS = "pipeinspect";   // >= 8 chars

// Access point rather than joining a network: at a manhole there is no
// network to join. The laptop associates to the camera node directly, or to
// the base node if you bridge them.
#define USE_ACCESS_POINT  1

// ---------------------------------------------------------- pin maps ----
#if defined(CAMERA_MODEL_AI_THINKER)
  #define PWDN_GPIO_NUM 32
  #define RESET_GPIO_NUM -1
  #define XCLK_GPIO_NUM  0
  #define SIOD_GPIO_NUM 26
  #define SIOC_GPIO_NUM 27
  #define Y9_GPIO_NUM   35
  #define Y8_GPIO_NUM   34
  #define Y7_GPIO_NUM   39
  #define Y6_GPIO_NUM   36
  #define Y5_GPIO_NUM   21
  #define Y4_GPIO_NUM   19
  #define Y3_GPIO_NUM   18
  #define Y2_GPIO_NUM    5
  #define VSYNC_GPIO_NUM 25
  #define HREF_GPIO_NUM 23
  #define PCLK_GPIO_NUM 22
  #define LAMP_GPIO_NUM  4    // the bright white LED; doubles as pipe lighting
#elif defined(CAMERA_MODEL_XIAO_ESP32S3)
  #define PWDN_GPIO_NUM -1
  #define RESET_GPIO_NUM -1
  #define XCLK_GPIO_NUM 10
  #define SIOD_GPIO_NUM 40
  #define SIOC_GPIO_NUM 39
  #define Y9_GPIO_NUM   48
  #define Y8_GPIO_NUM   11
  #define Y7_GPIO_NUM   12
  #define Y6_GPIO_NUM   14
  #define Y5_GPIO_NUM   16
  #define Y4_GPIO_NUM   18
  #define Y3_GPIO_NUM   17
  #define Y2_GPIO_NUM   15
  #define VSYNC_GPIO_NUM 38
  #define HREF_GPIO_NUM 47
  #define PCLK_GPIO_NUM 13
  #define LAMP_GPIO_NUM -1
#endif

// ------------------------------------------------- the quality ladder ---
struct Tier { framesize_t size; int jpeg_quality; const char *name; };

static const Tier TIERS[] = {
  { FRAMESIZE_UXGA,  10, "1600x1200 defect grading" },
  { FRAMESIZE_SVGA,  12, "800x600 crack detection"  },
  { FRAMESIZE_VGA,   14, "640x480 joint survey"     },
  { FRAMESIZE_VGA,   20, "640x480 general condition"},
  { FRAMESIZE_QVGA,  16, "320x240 navigation"       },
  { FRAMESIZE_QVGA,  28, "320x240 driving only"     },
};
static const int N_TIERS = sizeof(TIERS) / sizeof(TIERS[0]);
static int currentTier = 2;

static httpd_handle_t server = NULL;

#define PART_BOUNDARY "breadcrumbframe"
static const char *STREAM_TYPE =
    "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char *STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char *STREAM_PART =
    "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

// ============================================================ handlers ===

static esp_err_t stream_handler(httpd_req_t *req) {
  esp_err_t res = httpd_resp_set_type(req, STREAM_TYPE);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  char part[64];
  while (true) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) { res = ESP_FAIL; break; }

    size_t len = snprintf(part, sizeof(part), STREAM_PART, fb->len);
    res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, part, len);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    if (res != ESP_OK) break;      // client went away
  }
  return res;
}

static esp_err_t jpg_handler(httpd_req_t *req) {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) { httpd_resp_send_500(req); return ESP_FAIL; }
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  return res;
}

static esp_err_t tier_handler(httpd_req_t *req) {
  char query[32], value[8];
  int idx = currentTier;
  if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK &&
      httpd_query_key_value(query, "i", value, sizeof(value)) == ESP_OK) {
    idx = atoi(value);
  }
  if (idx < 0) idx = 0;
  if (idx >= N_TIERS) idx = N_TIERS - 1;
  currentTier = idx;

  sensor_t *s = esp_camera_sensor_get();
  s->set_framesize(s, TIERS[idx].size);
  s->set_quality(s, TIERS[idx].jpeg_quality);

  char body[128];
  int n = snprintf(body, sizeof(body), "{\"tier\":%d,\"name\":\"%s\"}",
                   idx, TIERS[idx].name);
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, body, n);
}

static esp_err_t lamp_handler(httpd_req_t *req) {
#if LAMP_GPIO_NUM >= 0
  char query[32], value[8];
  int on = 1;
  if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK &&
      httpd_query_key_value(query, "on", value, sizeof(value)) == ESP_OK) {
    on = atoi(value);
  }
  digitalWrite(LAMP_GPIO_NUM, on ? HIGH : LOW);
#endif
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, "{\"ok\":true}", 11);
}

static void startServer() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80;
  config.ctrl_port = 32768;

  httpd_uri_t jpg  = {"/jpg",  HTTP_GET, jpg_handler,  NULL};
  httpd_uri_t tier = {"/tier", HTTP_GET, tier_handler, NULL};
  httpd_uri_t lamp = {"/lamp", HTTP_GET, lamp_handler, NULL};
  if (httpd_start(&server, &config) == ESP_OK) {
    httpd_register_uri_handler(server, &jpg);
    httpd_register_uri_handler(server, &tier);
    httpd_register_uri_handler(server, &lamp);
  }

  // The stream gets its own server on :81 so a stalled stream cannot block
  // the control endpoints. This matters when the link is marginal.
  httpd_config_t sconfig = HTTPD_DEFAULT_CONFIG();
  sconfig.server_port = 81;
  sconfig.ctrl_port = 32769;
  httpd_handle_t stream = NULL;
  httpd_uri_t suri = {"/stream", HTTP_GET, stream_handler, NULL};
  if (httpd_start(&stream, &sconfig) == ESP_OK) {
    httpd_register_uri_handler(stream, &suri);
  }
}

// =============================================================== setup ===

void setup() {
  Serial.begin(115200);
#if LAMP_GPIO_NUM >= 0
  pinMode(LAMP_GPIO_NUM, OUTPUT);
  digitalWrite(LAMP_GPIO_NUM, HIGH);
#endif

  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;   config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM; config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;   config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = TIERS[currentTier].size;
  config.jpeg_quality = TIERS[currentTier].jpeg_quality;
  config.fb_count = psramFound() ? 2 : 1;
  config.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;   // newest frame wins: lower latency

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("EVT,0,camera init failed 0x%x\n", err);
    delay(3000);
    ESP.restart();
  }

#if USE_ACCESS_POINT
  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASS, 6);
  IPAddress ip = WiFi.softAPIP();
#else
  WiFi.begin("your-ssid", "your-password");
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print('.'); }
  IPAddress ip = WiFi.localIP();
#endif

  startServer();
  Serial.printf("\nCAM,ready,http://%s:81/stream\n", ip.toString().c_str());
  Serial.printf("CAM,launch, python run.py --live --camera http://%s:81/stream\n",
                ip.toString().c_str());
}

void loop() {
  delay(1000);
}
