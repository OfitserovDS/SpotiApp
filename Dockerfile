FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY package*.json ./
RUN npm ci
COPY index.html build.mjs tsconfig.json ./
COPY src ./src
RUN npm run build

FROM python:3.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 SETCRATE_DATA=/data
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 setcrate && install -d -o setcrate -g setcrate /data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY setcrate ./setcrate
COPY --from=frontend /build/dist ./dist
USER setcrate
EXPOSE 8765
CMD ["uvicorn", "setcrate.web:app", "--host", "0.0.0.0", "--port", "8765", "--no-access-log"]
