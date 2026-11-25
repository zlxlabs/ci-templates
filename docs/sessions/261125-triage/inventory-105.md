# #105 共享资源清单

| 资源 | 写入方 | 读取方 | 守卫 | 文件:行 |
| --- | --- | --- | --- | --- |
| 部署目录内受管配置文件 | install_release_files | find_bound_services、verify_live_files | 主机锁 | scripts/release_files.sh:110 |
| last_good_release | release_deploy promote | release_files_skip_forward、回滚 | 主机锁 | scripts/release_deploy.sh:623 |
| files/<sha>.tar 缓存 | prepare_release_files | 回滚 | 无（锁外 staging） | scripts/release_files.sh:97 |
