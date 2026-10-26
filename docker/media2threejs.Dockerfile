# Lean image for the media2threejs task: only what the run needs.
#   Claude Code 2.1.310 native binary; node 20 + npm (Three.js/Vite projects); python 3.11 + playwright 1.60
#   (pinned to the bundled chromium-headless-shell-1223), numpy, opencv-headless, pillow; ffmpeg; the skill.
# Dropped vs agent-scaffold-media2threejs:2.1.310 (7.6 GB): the evaluation MCP servers (playwright MCP, review,
# score), the npm copy of Claude Code, the full Chromium build, and duplicated browser copies.
# Build on the A800 (dead daemon proxy; build containers need the proxy vars blanked):
#   git -C <skill checkout> archive --format=tar --prefix=media2threejs/ HEAD > docker/media2threejs.tar
#   docker build --network host --build-arg http_proxy= --build-arg https_proxy= --build-arg HTTP_PROXY= \
#     --build-arg HTTPS_PROXY= --build-arg SKILL_COMMIT=<sha> -f docker/media2threejs.Dockerfile \
#     -t agent-scaffold-media2threejs:lean docker
FROM agent-scaffold-claude-code:2.1.310 AS src

FROM node:20-slim
ARG SKILL_COMMIT=unknown
ARG DEBIAN_MIRROR=http://mirrors.aliyun.com/debian
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
USER root
RUN sed -i "s#http://deb.debian.org/debian#${DEBIAN_MIRROR}#g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg ca-certificates curl git \
    && pip install --no-cache-dir --break-system-packages -i ${PIP_INDEX} \
         playwright==1.60.0 numpy opencv-python-headless pillow \
    && PLAYWRIGHT_BROWSERS_PATH=/ms-playwright python3 -m playwright install-deps chromium-headless-shell \
    && apt-get clean && rm -rf /var/lib/apt/lists/* /root/.cache
COPY --from=src /opt/claude-code /opt/claude-code
COPY --from=src /home/node/.cache/ms-playwright/chromium_headless_shell-1223 /ms-playwright/chromium_headless_shell-1223
COPY --from=src /home/node/.cache/ms-playwright/ffmpeg-1011 /ms-playwright/ffmpeg-1011
ADD media2threejs.tar /opt/skills/
RUN ln -sfn /opt/claude-code/claude /usr/local/bin/claude && chmod -R a+rX /ms-playwright /opt/skills \
    && claude --version
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
LABEL media2threejs.commit=$SKILL_COMMIT
USER node
WORKDIR /home/node
