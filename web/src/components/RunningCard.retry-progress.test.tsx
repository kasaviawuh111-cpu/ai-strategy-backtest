import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { RunningCard } from './SummaryCards';

describe('RunningCard retry progress', () => {
  it('keeps the original running card cancellable and replaces retry text on recovery', () => {
    // Component-only state fixture, not a live browser or real backtest assertion.
    const onCancel = vi.fn();
    const retryLabel = '数据获取遇到临时问题，正在自动重试（1/2）。原方案已保留，无需重新提交。';
    const recoveredLabel = '数据请求已恢复，正在继续读取。';
    const { rerender } = render(
      <RunningCard phase="running:data" progressLabel={retryLabel} onCancel={onCancel} />,
    );

    expect(screen.getByText(retryLabel)).toBeVisible();
    expect(screen.queryByText(/回测失败/)).not.toBeInTheDocument();
    const cancelButton = screen.getByRole('button', { name: '取消回测' });
    expect(cancelButton).toBeEnabled();
    fireEvent.click(cancelButton);
    expect(onCancel).toHaveBeenCalledTimes(1);

    rerender(
      <RunningCard phase="running:data" progressLabel={recoveredLabel} onCancel={onCancel} />,
    );
    expect(screen.queryByText(retryLabel)).not.toBeInTheDocument();
    expect(screen.getByText(recoveredLabel)).toBeVisible();
    expect(screen.queryByText(/回测失败/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '取消回测' })).toBe(cancelButton);
    expect(cancelButton).toBeEnabled();
  });
});
