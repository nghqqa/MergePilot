import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { ConfigProvider as AntConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App.jsx';

// 根级错误边界：生产构建也把渲染错误如实显示（诊断与诚实失败）
class RootBoundary extends React.Component {
  constructor(props) { super(props); this.state = { err: null }; }
  static getDerivedStateFromError(err) { return { err }; }
  render() {
    if (this.state.err) {
      return (
        <div style={{ fontFamily: 'monospace', padding: 24, whiteSpace: 'pre-wrap' }}>
          <h2>渲染错误（如实显示）</h2>
          <pre>{String(this.state.err && this.state.err.stack || this.state.err)}</pre>
        </div>
      );
    }
    return this.props.children;
  }
}
import { mpTheme } from './theme.js';
import './console.css';

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <RootBoundary>
      <AntConfigProvider theme={mpTheme} locale={zhCN}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AntConfigProvider>
    </RootBoundary>
  </React.StrictMode>
);
