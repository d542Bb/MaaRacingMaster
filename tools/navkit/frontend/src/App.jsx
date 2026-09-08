import React, { useEffect, useState } from 'react';
import { Layout, Nav, Tag, Modal, Select, Toast, Button } from '@douyinfe/semi-ui';
import {
  IconHistogram, IconDesktop, IconImage, IconVideo, IconServer, IconEdit,
  IconClose,
} from '@douyinfe/semi-icons';
import GraphView from './GraphView';
import CalibView from './CalibView';
import TemplatesView from './TemplatesView';
import ReplayView from './ReplayView';
import AssetsView from './AssetsView';
import PolicyView from './PolicyView';
import Inspector from './Inspector';
import { api, switchModule, shutdownServer } from './api';

const { Header, Sider, Content, Footer } = Layout;

const NAV_ITEMS = [
  { itemKey: 'graph', text: '路径树', icon: <IconHistogram /> },
  { itemKey: 'calib', text: 'ROI 校准', icon: <IconDesktop /> },
  { itemKey: 'tpl', text: '模板库', icon: <IconImage /> },
  { itemKey: 'replay', text: '会话回放', icon: <IconVideo /> },
  { itemKey: 'assets', text: '资产 v3', icon: <IconServer /> },
  { itemKey: 'policy', text: '策略', icon: <IconEdit /> },
];

const VIEW_KEYS = NAV_ITEMS.map(i => i.itemKey);
// hash 路由：#/graph、#/calib…—— 网址随视图变化，但始终是同一个页签（SPA 手感）
function viewFromHash() {
  const h = (window.location.hash || '').replace(/^#\/?/, '');
  return VIEW_KEYS.includes(h) ? h : 'graph';
}

export default function App() {
  const [view, setViewState] = useState(viewFromHash);
  const [selectedNode, setSelectedNode] = useState(null);
  const [traceRows, setTraceRows] = useState([]);
  const [graphDoc, setGraphDoc] = useState(null);
  const [policyDirty, setPolicyDirty] = useState(false);
  const [module, setModule] = useState(null);       // 当前编辑模块（server 端真值）
  const [moduleOptions, setModuleOptions] = useState([]);
  const [switching, setSwitching] = useState(false);

  // 模块切换后重拉全局数据（trace / graph 都按当前模块派生）
  useEffect(() => {
    api.trace().then(setTraceRows).catch(() => setTraceRows([]));
    api.graph().then(setGraphDoc).catch(() => setGraphDoc(null));
  }, [module]);

  useEffect(() => {
    api.modules()
      .then(({ current, available }) => {
        setModule(current);
        setModuleOptions(available);
      })
      .catch(() => setModule(null));
  }, []);

  useEffect(() => {
    const onHash = () => setViewState(viewFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  // 切视图 = 写 hash，由 hashchange 统一驱动状态（浏览器前进/后退可用）；
  // 策略页有未保存更改时 Modal 二次确认，确认（丢弃）后才写 hash
  const setView = (key) => {
    if (view !== 'policy' || !policyDirty) {
      window.location.hash = '/' + key;
      return;
    }
    Modal.confirm({
      title: '策略编辑尚未保存',
      content: '切换视图将丢弃未保存的更改（draft 不会被写盘）。确定离开？',
      okText: '丢弃并离开',
      cancelText: '留下继续编辑',
      onOk: () => { window.location.hash = '/' + key; },
    });
  };

  // 切换编辑模块：server 重建 StudioState；成功后以 module 为 key 重挂载全部视图
  // （各视图内部数据自拉，key 变化即强制重置），未保存的策略 draft 同样弹确认。
  const doSwitchModule = (next) => {
    if (!next || next === module || switching) return;
    const apply = () => {
      setSwitching(true);
      switchModule(next)
        .then((res) => {
          if (!res.ok) {
            Toast.error(`切换失败：${res.error || '未知错误'}`);
            return;
          }
          setModule(next);
          setSelectedNode(null);
          setPolicyDirty(false);
          Toast.success(`已切换到 ${next}`);
        })
        .catch((e) => Toast.error(`切换失败：${e.message}`))
        .finally(() => setSwitching(false));
    };
    if (view === 'policy' && policyDirty) {
      Modal.confirm({
        title: '策略编辑尚未保存',
        content: `切换到模块 ${next} 将丢弃未保存的更改（draft 不会被写盘）。确定切换？`,
        okText: '丢弃并切换',
        cancelText: '留下继续编辑',
        onOk: apply,
      });
      return;
    }
    apply();
  };

  // 关闭 server 进程：调 /api/shutdown → server 发 200 后会 os._exit。
  // 手动 open 的浏览器 tab 不受 JS 控制（window.close 会被拦截），到时 toast 提示即可。
  const doShutdown = () => {
    Modal.confirm({
      title: '退出 NavKit Studio',
      content: '退出后 server 进程会自动结束。下次需要再双击 start_navkit.ps1（或 taskkill 后重起）启动。本浏览器页面可以手动关闭。',
      okText: '退出',
      cancelText: '留下',
      okButtonProps: { type: 'danger' },
      onOk: async () => {
        await shutdownServer();
        Toast.success('server 已关闭，可以关闭本页面');
        // 试一下窗口关闭——用户手动开的 tab 会被浏览器拒绝，没关系，toast 已说明
        setTimeout(() => { try { window.close(); } catch { /* noop */ } }, 200);
      },
    });
  };

  return (
    <Layout className="app">
      <Header className="topbar">
        <div className="brand">
          <span className="brand-dot" />
          <span className="brand-name">NavKit Studio</span>
          <Select
            size="small"
            value={module}
            loading={module === null}
            disabled={switching}
            style={{ width: 120 }}
            onChange={doSwitchModule}
            optionList={moduleOptions.map((m) => ({ value: m, label: m }))}
          />
          <Tag size="small" color="violet" style={{ margin: 0 }}>schema v3</Tag>
          <Button
            size="small"
            type="tertiary"
            theme="light"
            icon={<IconClose />}
            onClick={doShutdown}
            style={{ marginLeft: 4 }}
          >
            退出
          </Button>
        </div>
        <div style={{ flex: 1 }} />
      </Header>

      <Layout style={{ flex: 1, minHeight: 0 }}>
        <Sider style={{ background: 'var(--semi-color-bg-1)' }}>
          <Nav
            style={{ height: '100%', maxWidth: 176 }}
            items={NAV_ITEMS}
            selectedKeys={[view]}
            onSelect={({ itemKey }) => setView(itemKey)}
            footer={{ collapseButton: true }}
          />
        </Sider>

        <Layout style={{ flex: 1, minHeight: 0 }}>
          <Content style={{ minHeight: 0, display: 'flex' }}>
            <div className="content-main" key={module}>
              {view === 'graph' && (
                <GraphView traceRows={traceRows} onSelectNode={setSelectedNode} />
              )}
              {view === 'calib' && <CalibView />}
              {view === 'tpl' && <TemplatesView />}
              {view === 'replay' && <ReplayView traceRows={traceRows} />}
              {view === 'assets' && <AssetsView graphDoc={graphDoc} />}
              {view === 'policy' && <PolicyView onDirtyChange={setPolicyDirty} />}
            </div>
            {view === 'graph' && (
              <Inspector node={selectedNode} traceRows={traceRows} />
            )}
          </Content>

          <Footer className="statusbar">
            <span>{module ? `${module}_assets.json` : '…'}</span>
            <span>{graphDoc ? `${graphDoc.nodes.length} 节点 · ${graphDoc.edges.length} 边` : '加载中…'}</span>
            {graphDoc && <span>孤儿 {graphDoc.orphans.length} · 未担保 {graphDoc.unguarded_points.length}</span>}
            <span>trace {traceRows.length} 行</span>
            <div style={{ flex: 1 }} />
            <span>NavKit 控制台 · 构建版</span>
          </Footer>
        </Layout>
      </Layout>
    </Layout>
  );
}