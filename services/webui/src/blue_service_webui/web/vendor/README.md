# 前端第三方依赖

这些文件是随包分发的**离线静态资源**，不经过 CDN、不需要构建步骤。
升级时按下方版本重新 `npm pack` 并替换同名文件即可。

| 目录 | 库 | 版本 | 许可证 | 用途 |
| --- | --- | --- | --- | --- |
| `marked/` | [marked](https://github.com/markedjs/marked) | 14.1.3 | MIT（见 `marked/LICENSE.md`） | GFM Markdown 解析 |
| `katex/` | [KaTeX](https://katex.org/) | 0.16.11 | MIT（见 `katex/LICENSE`） | LaTeX 数学公式排版 |

## 来源与更新方式

```bash
npm pack marked@14.1.3 katex@0.16.11
tar xzf marked-14.1.3.tgz katex-0.16.11.tgz

# marked
cp package/marked.min.js marked/

# KaTeX：只需 woff2 字体（现代浏览器均支持），不带 woff/ttf 以控制体积
cp package/dist/katex.min.js package/dist/katex.min.css katex/
cp package/dist/fonts/*.woff2 katex/fonts/
```

## 约束

- `katex.min.css` 以相对路径 `fonts/` 引用字体，目录结构不能打散；
- 两者都是 UMD 构建，通过普通 `<script defer>` 挂到 `window.marked` / `window.katex`；
- `app.js` 在库缺失时会退化为内置的简易 Markdown 渲染器，页面不会白屏；
- 新增或升级依赖时必须同步更新本文件与上表版本号。
