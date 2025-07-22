CUDA_VISIBLE_DEVICES=0 python demo/demo.py \
    --config-file configs/TESTR/TotalText/TESTR_R_50_Polygon.yaml \
    --input datasets/totaltext/test_images \
    --output ./output/pretrain/totaltext/TESTR_R50_BASELINE/val_result \
    --opts MODEL.WEIGHTS weights/totaltext_testr_R_50_polygon.pth MODEL.TRANSFORMER.INFERENCE_TH_TEST 0.3