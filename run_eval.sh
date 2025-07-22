## released TESTR checkpoint weight
# CUDA_VISIBLE_DEVICES=0 python tools/train_net.py --config-file configs/TESTR/TotalText/TESTR_R_50_Polygon.yaml --eval-only MODEL.WEIGHTS weights/totaltext_testr_R_50_polygon.pth


## TESTR_SD21
# CUDA_VISIBLE_DEVICES=0 python tools/train_net.py --config-file configs/SD21/Pretrain/TESTR_SD21_Polygon.yaml --eval-only MODEL.WEIGHTS output/pretrain/totaltext/TESTR_SD21_lr1e4_bs2/model_0089999.pth


## TESTR_R50
CUDA_VISIBLE_DEVICES=0 python tools/train_net.py --config-file configs/TESTR/Pretrain/TESTR_R_50_Polygon.yaml --eval-only MODEL.WEIGHTS output/pretrain/totaltext/TESTR_R50_lr1e4_bs2/model_0079999.pth