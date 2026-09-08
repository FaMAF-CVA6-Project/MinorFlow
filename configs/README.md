# Configurations

`gem5_config_MinorFlow.py` and `gem5_config_Reference_Core.py` are this viewer's own configurations, and run anywhere the repository is checked out.

## Reaching the CVA6 fork's configurations

The matched configuration and `MinorCPU_CVA6.patch` live in `gem5_config_CVA6/gem5/configs/` in the [CVA6 fork](https://github.com/FaMAF-CVA6-Project/CVA6), which is the repository this one sits inside as a submodule.

Inside the fork it is still a convenience, so make it locally if you want it:

```bash
ln -s ../../../gem5_config_CVA6/gem5/configs configs/CVA6
```
