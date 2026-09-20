# GitHub Actions 常见失败模式

## 测试失败

### Python 测试

**错误特征**:
```
FAILED tests/test_module.py::TestClassName::test_method - AssertionError: Expected 5, got 3
pytest exit code: 1
```

**常见原因**:
- 断言失败
- Mock 配置错误
- 测试数据问题
- 环境差异

**分析要点**:
- 查看具体失败的测试方法
- 检查期望值 vs 实际值
- 确认测试环境配置
- 查看是否为 flaky test

### JavaScript/TypeScript 测试

**错误特征**:
```
FAIL src/components/Button.test.tsx
  ● should render correctly
    Expected: "Click me"
    Received: "Click"
```

**常见原因**:
- 组件渲染问题
- 异步测试超时
- Mock 函数配置错误
- DOM 操作失败

## 依赖安装失败

### Python pip

**错误特征**:
```
ERROR: Could not install packages due to an EnvironmentError: [Errno 28] No space left on device
ERROR: Failed building wheel for package-name
```

**常见原因**:
- 磁盘空间不足
- 编译工具链缺失
- 网络超时
- 版本冲突

**解决方案**:
```yaml
- name: Free disk space
  run: |
    sudo rm -rf /var/lib/apt/lists/*
    docker system prune -f
```

### Node.js npm/yarn

**错误特征**:
```
npm ERR! code ENOSPC
npm ERR! errno -28
npm ERR! syscall mkdir
```

**常见原因**:
- 磁盘空间不足
- npm 缓存问题
- 依赖版本冲突
- 网络问题

## 构建失败

### Python 构建

**错误特征**:
```
mypy src/main.py: error: Argument 1 to "function" has incompatible type "str"
flake8 src/main.py:1:1: E302 expected 2 blank lines
```

**常见原因**:
- 类型检查失败
- 代码格式问题
- 语法错误
- 导入错误

### JavaScript/TypeScript 构建

**错误特征**:
```
TS2322: Type 'string' is not assignable to type 'number'.
ESLint: 'console.log' is disallowed.
```

**常见原因**:
- TypeScript 类型错误
- ESLint 规则违反
- Webpack 配置问题
- 依赖版本不兼容

## 环境问题

### 权限错误

**错误特征**:
```
Permission denied: '/usr/local/bin/script'
sudo: a terminal is required to read the password
```

**常见原因**:
- 脚本执行权限
- Docker 用户权限
- 系统目录访问限制

**解决方案**:
```yaml
- name: Fix permissions
  run: |
    chmod +x scripts/deploy.sh
    sudo chown -R $USER:$USER /path/to/directory
```

### 资源限制

**错误特征**:
```
Error: Container runtime is out of memory
Killed signal 9 (SIGKILL)
```

**常见原因**:
- 内存不足
- CPU 超限
- 磁盘空间不足
- 超时限制

**解决方案**:
```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    resources:
      memory: 4GB
```

## 部署失败

### Docker 部署

**错误特征**:
```
ERROR: Service 'app' failed to build: failed to solve: process "/bin/sh -c pip install" did not complete successfully
docker: Error response from daemon: pull access denied
```

**常见原因**:
- Dockerfile 错误
- 镜像拉取失败
- 网络连接问题
- 认证失败

### 云服务部署

**错误特征**:
```
AWS: InvalidClientTokenId
GCP: Permission denied
Azure: Resource not found
```

**常见原因**:
- 认证配置错误
- 权限不足
- 资源不存在
- 配置文件错误

## 缓存问题

### 缓存失效

**错误特征**:
```
Cache not found for input keys
Cache restored from key: failed
```

**常见原因**:
- 缓存键不匹配
- 缓存大小超限
- 缓存版本变化

**解决方案**:
```yaml
- name: Cache dependencies
  uses: actions/cache@v3
  with:
    path: ~/.cache/pip
    key: \${{ runner.os }}-pip-\${{ hashFiles('**/requirements.txt') }}
    restore-keys: |
      \${{ runner.os }}-pip-
```

## 网络问题

### 连接超时

**错误特征**:
```
Request timeout after 30 seconds
Connection refused
SSL: SSLV3_ALERT_HANDSHAKE_FAILURE
```

**常见原因**:
- 网络连接不稳定
- 代理配置问题
- SSL 证书问题
- 防火墙限制

**解决方案**:
```yaml
- name: Configure network
  run: |
    git config --global http.proxy http://proxy.company.com:8080
    export NODE_TLS_REJECT_UNAUTHORIZED=0
```

## 并发问题

### 资源竞争

**错误特征**:
```
Resource busy
Lock acquisition failed
Database connection limit exceeded
```

**常见原因**:
- 多个 job 同时访问资源
- 数据库连接池耗尽
- 文件锁定冲突

**解决方案**:
```yaml
jobs:
  deploy:
    needs: build
    runs-on: ubuntu-latest
    concurrency: deploy
```

## 配置错误

### Workflow 配置

**错误特征**:
```
Invalid workflow file: .github/workflows/ci.yml
Error: Can't find 'action.yml'
```

**常见原因**:
- YAML 语法错误
- Action 版本不兼容
- 环境变量未定义
- 路径配置错误

### 环境变量

**错误特征**:
```
Error: Required environment variable 'API_KEY' is not set
undefined variable 'NODE_ENV'
```

**常见原因**:
- Secrets 未配置
- 环境变量名称错误
- 作用域配置问题

**解决方案**:
```yaml
env:
  API_KEY: \${{ secrets.API_KEY }}
  NODE_ENV: production
```

## 故障排查清单

### 1. 快速诊断
- [ ] 检查 run 状态和持续时间
- [ ] 查看失败 job 和 step
- [ ] 扫描错误消息关键词
- [ ] 确认最近的代码变更

### 2. 深入分析
- [ ] 查看完整日志
- [ ] 检查依赖版本
- [ ] 验证环境配置
- [ ] 对比成功/失败 run

### 3. 根本原因
- [ ] 识别失败模式
- [ ] 分析触发条件
- [ ] 查看历史趋势
- [ ] 确认影响范围

### 4. 修复验证
- [ ] 实施修复方案
- [ ] 触发测试 run
- [ ] 验证修复效果
- [ ] 更新文档和流程