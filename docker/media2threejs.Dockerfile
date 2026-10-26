# Claude Code 2.1.310 + what the media2threejs skill needs to rebuild a video as a rendered Three.js scene.
# Build on the A800 (its docker daemon proxy is dead, and ~/.docker/config.json injects dead proxies into builds):
#   git -C <skill checkout> archive --format=tar --prefix=media2threejs/ HEAD > docker/media2threejs.tar
#   docker build --network host --build-arg http_proxy= --build-arg https_proxy= --build-arg HTTP_PROXY= \
#     --build-arg HTTPS_PROXY= --build-arg SKILL_COMMIT=<sha> -f docker/media2threejs.Dockerfile \
#     -t agent-scaffold-media2threejs:2.1.310 docker
FROM agent-scaffold-claude-code:2.1.310
ARG SKILL_COMMIT=unknown
USER root
# Python playwright pinned to the browser build already in the image (chromium-1223 = playwright 1.60);
# numpy/opencv/pillow for frame analysis. Aliyun mirror: direct PyPI is slow from the servers.
RUN pip install --no-cache-dir --break-system-packages -i https://mirrors.aliyun.com/pypi/simple/ \
      playwright==1.60.0 numpy opencv-python-headless pillow \
    && cp -a /home/node/.cache/ms-playwright /ms-playwright && chmod -R a+rX /ms-playwright
# The workspace is bind-mounted over /home/node, so browsers and the skill live outside it.
ADD media2threejs.tar /opt/skills/
RUN chmod -R a+rX /opt/skills
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
LABEL media2threejs.commit=$SKILL_COMMIT
USER node
