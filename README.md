<div align="center">

# 🚀 Nexus-Argo
### 现代化双核自动化部署与 Argo 隧道优选裂变订阅系统

[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live-brightgreen?style=flat-square&logo=github)](https://sid-x2026.github.io/Nexus-Argo/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20VPS-orange?style=flat-square)](https://github.com/Sid-x2026/Nexus-Argo)

[🌐 在线可视化控制面板](https://sid-x2026.github.io/Nexus-Argo/) | [📖 快速上手指南](#-快速部署与使用) | [💬 问题反馈](https://github.com/Sid-x2026/Nexus-Argo/issues)

</div>

---

## 💡 项目简介

**Nexus-Argo** 是一款专为 Linux VPS 打造的高效、轻量级代理运维管理工具。项目创新性地采用“前端网页生成 + 后端一键脚本”的解耦架构，完美支持 Xray-core 与 Sing-box 双核运行模式，结合 Cloudflare Argo 隧道与多优选裂变订阅系统，让复杂的节点配置与部署变得极致简单、丝滑且安全。

---

## ✨ 核心特性

* 🌐 **网页端可视化配置**：通过精美的 Web 交互面板实时调整参数、一键生成专属安装命令与批量节点订阅。
* ⚡ **双核灵活切换**：支持 Xray 与 Sing-box 多重现代传输协议。
* 🚇 **智能 Argo 隧道**：内置 Argo 隧道回源与域名映射支持，隐藏真实服务器 IP，提升连接稳定与安全性。
* 🛡️ **一键运维与卸载**：配套完善的维护与一键彻底卸载清理机制，不留半点残留缓存。
* ☁️ **零成本高性能托管**：前端静态面板基于 GitHub Pages 稳定托管，访问秒开、永不掉线。

---

## 🛠️ 快速部署与使用

### 第一步：访问在线可视化控制面板
前往网页端选择对应内核、填写域名和 Token：
👉 [https://sid-x2026.github.io/Nexus-Argo/](https://sid-x2026.github.io/Nexus-Argo/)

### 第二步：执行一键安装脚本
在网页端右侧复制自动生成的专属安装命令，登录您的 VPS 运行即可：
(请前往网页控制面板复制完整带参数的命令，或直接运行下方基础安装)

### 第三步：一键卸载与清理（如需）
如果需要彻底清理并卸载服务器上的所有相关服务与容器，可随时执行以下卸载命令：
bash <(curl -Ls https://raw.githubusercontent.com/Sid-x2026/Nexus-Argo/main/install.sh) uninstall

---

### 📁 仓库目录架构

```text
Nexus-Argo/
├── .github/
│   └── workflows/
│       └── pages.yml # GitHub Pages 自动化构建与持续部署流水线
├── web/
│   └── index.html # 响应式高颜值 Web 运维控制面板前端
└── install.sh # 核心一键安装、环境检测、容器集群与卸载运维脚本

---

## ⚠️ 免责声明

* 本项目仅供技术学习、安全研究与网络优化交流使用，请严格遵守当地法律法规。

---

## 📝 许可协议

本项目基于 [MIT License](LICENSE) 开源协议进行分发与使用。
