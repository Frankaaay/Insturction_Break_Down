# 本地录制视频样本

把本地录制的原子操作视频放在这个目录。视频、标注 manifest 和模型运行产物默认不会被 Git 跟踪；本文件仅用于保留目录和说明约定。

建议第一批“拿起水壶”样本至少包含：

- `pick_kettle_success_01.mp4`：正常拿起并稳定保持。
- `pick_kettle_in_progress_01.mp4`：视频结束时仍在接近、调整或抓取。
- `pick_kettle_drop_retry_01.mp4`：中途滑脱或掉落，但继续重新尝试。
- `pick_kettle_wrong_object_01.mp4`：拿起了水壶以外的物体。
- `pick_kettle_occluded_01.mp4`：关键接触或最终状态被遮挡。

拍摄建议：

- 使用固定机位，完整拍到水壶、手以及水壶原本的支撑面。
- 操作开始前保留约 1 秒静止画面，操作完成后保留约 2 秒稳定结果。
- 第一轮使用 MP4/H.264、无须录音；原始帧率可保留，runner 会生成评测输入。
- 不要为了凑整秒裁掉关键事件；runner 会自动添加视频结尾检查点。

复制 `monitor/data_manifest.example.json` 为本目录下的 `manifest.json`，然后把每个 `video` 写成相对于本目录的文件名，例如：

```json
{
  "schema_version": 1,
  "cases": [
    {
      "id": "pick_kettle_success_01",
      "video": "pick_kettle_success_01.mp4",
      "atomic_action": "拿起水壶",
      "object": "水壶",
      "target": "水壶离开支撑面并稳定保持在手中",
      "initial_state": "水壶放在桌面上，手尚未接触水壶",
      "success_criteria": [
        "水壶明显离开桌面",
        "水壶由手稳定保持"
      ],
      "timeout_s": 20
    }
  ]
}
```
