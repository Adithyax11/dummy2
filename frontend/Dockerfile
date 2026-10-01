FROM node:20-alpine
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund --fetch-retries=5 --fetch-timeout=120000
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev"]