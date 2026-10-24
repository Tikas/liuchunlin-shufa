# 刘春霖小楷书房 · 在线诗词书法生成系统

- **在线前端访问地址（GitHub Pages）**：<https://dvbs2000.github.io/liuchunlin-shufa/>
- **核心架构**：
  1. **前端（GitHub Pages 静态托管）**：位于 `docs/` 目录，提供古风宣纸交互、智能题名/作者/体裁识别、以及经典名篇展示，**绝不包含任何 `.ttf` 字库文件**。
  2. **后端（Ubuntu 私有主机光栅化服务）**：位于 `server/` 目录，将 `v003` 核心字库锁存在私有服务器 `/opt/liuchunlin_private`（`chmod 600`），仅对外提供内存光栅化图片接口（`POST /api/render`），内置 **单 IP 每日最多生成 2 次** 限流与 **故宫宋锦装裱底边 + 微信长按识别朱砂印章二维码**。
