import React, { useEffect, useState } from 'react';
import { Spin } from '@douyinfe/semi-ui';

// ROI 校准：既有校准器（calibrator.html，vanilla JS）经 iframe 内嵌于控制台，
// 同页签同源；hash 路由下 URL 停留在 /#/calib，不再跳转新页面。
export default function CalibView({ onDirtyChange }) {
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const onMessage = (e) => {
      if (e.data?.source === 'navkit-calib') onDirtyChange?.(Boolean(e.data.dirty));
    };
    window.addEventListener('message', onMessage);
    return () => { window.removeEventListener('message', onMessage); onDirtyChange?.(false); };
  }, [onDirtyChange]);

  return (
    <div className="calib-view">
      {!loaded && (
        <div className="calib-loading"><Spin size="large" tip="加载校准器…" /></div>
      )}
      <iframe
        className="calib-frame"
        src="/calibrator.html"
        title="ROI 校准器"
        onLoad={() => setLoaded(true)}
      />
    </div>
  );
}